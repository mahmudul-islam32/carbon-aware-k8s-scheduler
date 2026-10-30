"""Unit tests for re-planning held jobs on forecast refresh (F-20)."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from prometheus_client import REGISTRY

from carbon_scheduler.config import (
    ANNOTATION_ACTUAL_START,
    ANNOTATION_CO2_AVOIDED,
    ANNOTATION_DEADLINE,
    ANNOTATION_ESTIMATED_RUNTIME,
    ANNOTATION_EXPECTED_RENEWABLE_SHARE,
    ANNOTATION_FLEXIBLE,
    ANNOTATION_LATEST_START,
    ANNOTATION_PLANNED_AT,
    ANNOTATION_PLANNED_START,
    ANNOTATION_POLICY,
    Settings,
)
from carbon_scheduler.controller import handlers
from carbon_scheduler.controller.annotations import parse_annotations
from carbon_scheduler.controller.handlers import handle_job_timer, set_scheduler
from carbon_scheduler.controller.scheduler import JobScheduler
from carbon_scheduler.grid.models import GridData, RenewableShareForecast

NOW = datetime(2026, 10, 29, 6, 0, tzinfo=UTC)

# Forecast A (first fetch): green plateau at +6h..+8h.
SHARES_A = [30, 30, 35, 40, 50, 60, 85, 85, 85, 50, 40, 35, 30, 30, 30, 30]
# Forecast B (refresh): a much greener plateau appears at +2h..+4h; +6h..+8h unchanged.
SHARES_B = [30, 30, 95, 95, 95, 60, 85, 85, 85, 50, 40, 35, 30, 30, 30, 30]
# Forecast B' (refresh): the earlier plateau is only 2 points greener -> below hysteresis.
SHARES_B_SMALL = [30, 30, 87, 87, 87, 60, 85, 85, 85, 50, 40, 35, 30, 30, 30, 30]


def _grid(shares: list[float], fetched_at: datetime, base: datetime = NOW, **kw: Any) -> GridData:
    forecast = RenewableShareForecast(
        unix_seconds=[int((base + timedelta(hours=i)).timestamp()) for i in range(len(shares))],
        ren_share=shares,
        substitute=kw.get("substitute", False),
    )
    return GridData(forecast=forecast, fetched_at=fetched_at, country="de")


def _scheduler(grid: GridData, **settings: Any) -> tuple[JobScheduler, MagicMock]:
    client = MagicMock()
    client.get_grid_data = AsyncMock(return_value=grid)
    scheduler = JobScheduler(settings=Settings(**settings))
    scheduler.grid_client = client
    return scheduler, client


def _job(base: datetime = NOW, deadline_h: float = 14, **extra: str) -> dict[str, str]:
    annotations = {
        ANNOTATION_FLEXIBLE: "true",
        ANNOTATION_DEADLINE: (base + timedelta(hours=deadline_h)).isoformat(),
        ANNOTATION_ESTIMATED_RUNTIME: "1h",
    }
    annotations.update(extra)
    return annotations


async def _plan(scheduler: JobScheduler, annotations: dict[str, str]) -> dict[str, str]:
    """Run the initial plan at NOW and return the Job's annotations afterwards."""
    decision = await scheduler.calculate_schedule("job", "replan-ns", annotations, current_time=NOW)
    assert not decision.should_release
    return {**annotations, **decision.annotations_to_set}


def _replans(outcome: str) -> float:
    value = REGISTRY.get_sample_value(
        "carbon_replan_decisions_total", {"namespace": "replan-ns", "outcome": outcome}
    )
    return value or 0.0


class TestPlannedAt:
    @pytest.mark.asyncio
    async def test_initial_plan_records_planned_at(self) -> None:
        scheduler, _ = _scheduler(_grid(SHARES_A, fetched_at=NOW))
        annotations = await _plan(scheduler, _job())
        assert annotations[ANNOTATION_PLANNED_AT] == NOW.isoformat()
        assert parse_annotations(annotations).planned_at == NOW


class TestReplanTwoForecasts:
    @pytest.mark.asyncio
    async def test_moves_to_greener_window_after_refresh(self) -> None:
        scheduler, client = _scheduler(_grid(SHARES_A, fetched_at=NOW))
        annotations = await _plan(scheduler, _job())
        old_start = parse_annotations(annotations).planned_start
        assert old_start is not None and old_start >= NOW + timedelta(hours=6)

        refreshed = NOW + timedelta(minutes=45)
        client.get_grid_data.return_value = _grid(SHARES_B, fetched_at=refreshed)
        moved_before = _replans("moved")
        now = NOW + timedelta(hours=1)
        decision = await scheduler.replan("job", "replan-ns", parse_annotations(annotations), now)

        assert decision is not None and decision.moved
        assert decision.old_start == old_start
        assert NOW + timedelta(hours=2) <= decision.new_start <= NOW + timedelta(hours=3)
        assert decision.old_share_pct == pytest.approx(85.0)
        assert decision.new_share_pct == pytest.approx(95.0)
        assert decision.annotations_to_set[ANNOTATION_PLANNED_START] == decision.new_start.isoformat()
        assert decision.annotations_to_set[ANNOTATION_PLANNED_AT] == now.isoformat()
        assert decision.annotations_to_set[ANNOTATION_EXPECTED_RENEWABLE_SHARE] == "95.0"
        assert ANNOTATION_CO2_AVOIDED in decision.annotations_to_set
        assert _replans("moved") == moved_before + 1

    @pytest.mark.asyncio
    async def test_no_check_when_forecast_not_newer_than_plan(self) -> None:
        scheduler, _ = _scheduler(_grid(SHARES_A, fetched_at=NOW))
        annotations = await _plan(scheduler, _job())
        later = NOW + timedelta(minutes=10)
        assert await scheduler.replan("job", "replan-ns", parse_annotations(annotations), later) is None

    @pytest.mark.asyncio
    async def test_at_most_one_replan_per_refresh(self) -> None:
        scheduler, client = _scheduler(_grid(SHARES_A, fetched_at=NOW))
        annotations = await _plan(scheduler, _job())
        client.get_grid_data.return_value = _grid(SHARES_B, fetched_at=NOW + timedelta(minutes=30))

        first = await scheduler.replan(
            "job", "replan-ns", parse_annotations(annotations), NOW + timedelta(minutes=31)
        )
        assert first is not None and first.moved
        annotations.update(first.annotations_to_set)

        # Further reconcile ticks against the same forecast do nothing.
        for minutes in (32, 33, 50):
            again = await scheduler.replan(
                "job", "replan-ns", parse_annotations(annotations), NOW + timedelta(minutes=minutes)
            )
            assert again is None


class TestHysteresis:
    @pytest.mark.asyncio
    async def test_small_improvement_keeps_plan(self) -> None:
        scheduler, client = _scheduler(_grid(SHARES_A, fetched_at=NOW))
        annotations = await _plan(scheduler, _job())
        old_start = parse_annotations(annotations).planned_start
        client.get_grid_data.return_value = _grid(SHARES_B_SMALL, fetched_at=NOW + timedelta(hours=1))

        kept_before = _replans("kept")
        now = NOW + timedelta(hours=1, minutes=1)
        decision = await scheduler.replan("job", "replan-ns", parse_annotations(annotations), now)

        assert decision is not None and not decision.moved
        assert decision.new_start == old_start
        # Only planned-at advances, so the same refresh is not evaluated again.
        assert decision.annotations_to_set == {ANNOTATION_PLANNED_AT: now.isoformat()}
        assert _replans("kept") == kept_before + 1

    @pytest.mark.asyncio
    async def test_margin_is_configurable(self) -> None:
        scheduler, client = _scheduler(
            _grid(SHARES_A, fetched_at=NOW), replan_min_improvement_pct=1.0
        )
        annotations = await _plan(scheduler, _job())
        client.get_grid_data.return_value = _grid(SHARES_B_SMALL, fetched_at=NOW + timedelta(hours=1))
        decision = await scheduler.replan(
            "job", "replan-ns", parse_annotations(annotations), NOW + timedelta(hours=1, minutes=1)
        )
        assert decision is not None and decision.moved

    @pytest.mark.asyncio
    async def test_no_flapping_back_and_forth(self) -> None:
        scheduler, client = _scheduler(_grid(SHARES_A, fetched_at=NOW))
        annotations = await _plan(scheduler, _job())
        client.get_grid_data.return_value = _grid(SHARES_B, fetched_at=NOW + timedelta(minutes=30))
        first = await scheduler.replan(
            "job", "replan-ns", parse_annotations(annotations), NOW + timedelta(minutes=31)
        )
        assert first is not None and first.moved
        annotations.update(first.annotations_to_set)

        # Next refresh: the old window is now marginally greener than the new one.
        flip = [30, 30, 95, 95, 95, 60, 97, 97, 97, 50, 40, 35, 30, 30, 30, 30]
        client.get_grid_data.return_value = _grid(flip, fetched_at=NOW + timedelta(minutes=60))
        second = await scheduler.replan(
            "job", "replan-ns", parse_annotations(annotations), NOW + timedelta(minutes=61)
        )
        assert second is not None and not second.moved
        assert second.new_start == first.new_start


class TestLatestStartLimit:
    @pytest.mark.asyncio
    async def test_never_moved_past_latest_start(self) -> None:
        latest = NOW + timedelta(hours=5)
        parsed = parse_annotations(
            _job(
                **{
                    ANNOTATION_PLANNED_START: (NOW + timedelta(hours=2)).isoformat(),
                    ANNOTATION_LATEST_START: latest.isoformat(),
                    ANNOTATION_PLANNED_AT: NOW.isoformat(),
                }
            )
        )
        # Greener before latest-start (80 %), and greenest of all (99 %) only after it.
        shares = [30, 30, 50, 50, 80, 80, 99, 99, 99, 99, 99, 35, 30, 30, 30, 30]
        scheduler, _ = _scheduler(_grid(shares, fetched_at=NOW + timedelta(hours=1)))
        decision = await scheduler.replan(
            "job", "replan-ns", parsed, NOW + timedelta(hours=1, minutes=1)
        )
        assert decision is not None and decision.moved
        assert NOW + timedelta(hours=3) <= decision.new_start <= latest

    @pytest.mark.asyncio
    async def test_plan_at_latest_start_stays_there(self) -> None:
        # max-delay 5h puts latest-start (and the initial plan) at NOW+5h.
        scheduler, client = _scheduler(_grid(SHARES_A, fetched_at=NOW), max_delay="5h")
        parsed = parse_annotations(await _plan(scheduler, _job()))
        assert parsed.latest_start is not None and parsed.planned_start == parsed.latest_start
        late_green = [30, 30, 35, 40, 50, 60, 99, 99, 99, 99, 99, 35, 30, 30, 30, 30]
        client.get_grid_data.return_value = _grid(late_green, fetched_at=NOW + timedelta(hours=1))
        decision = await scheduler.replan(
            "job", "replan-ns", parsed, NOW + timedelta(hours=1, minutes=1)
        )
        assert decision is not None and not decision.moved
        assert decision.new_start == parsed.latest_start

    @pytest.mark.asyncio
    async def test_no_replan_once_due(self) -> None:
        scheduler, client = _scheduler(_grid(SHARES_A, fetched_at=NOW))
        annotations = await _plan(scheduler, _job())
        parsed = parse_annotations(annotations)
        assert parsed.planned_start is not None
        client.get_grid_data.return_value = _grid(SHARES_B, fetched_at=parsed.planned_start)
        assert await scheduler.replan("job", "replan-ns", parsed, parsed.planned_start) is None


class TestReplanGuards:
    @pytest.mark.asyncio
    async def test_disabled_by_setting(self) -> None:
        scheduler, client = _scheduler(_grid(SHARES_A, fetched_at=NOW), replan_enabled=False)
        annotations = await _plan(scheduler, _job())
        client.get_grid_data.return_value = _grid(SHARES_B, fetched_at=NOW + timedelta(hours=1))
        assert (
            await scheduler.replan("job", "replan-ns", parse_annotations(annotations), NOW + timedelta(hours=1))
            is None
        )

    @pytest.mark.asyncio
    async def test_degraded_or_missing_forecast_keeps_plan(self) -> None:
        scheduler, client = _scheduler(_grid(SHARES_A, fetched_at=NOW))
        parsed = parse_annotations(await _plan(scheduler, _job()))
        later = NOW + timedelta(hours=1)
        client.get_grid_data.return_value = _grid(SHARES_B, fetched_at=later, substitute=True)
        assert await scheduler.replan("job", "replan-ns", parsed, later) is None
        client.get_grid_data.return_value = None
        assert await scheduler.replan("job", "replan-ns", parsed, later) is None

    @pytest.mark.asyncio
    async def test_immediate_policy_is_not_replanned(self) -> None:
        scheduler, _ = _scheduler(_grid(SHARES_B, fetched_at=NOW + timedelta(hours=1)))
        parsed = parse_annotations(
            _job(
                **{
                    ANNOTATION_POLICY: "immediate",
                    ANNOTATION_PLANNED_START: (NOW + timedelta(hours=6)).isoformat(),
                }
            )
        )
        assert await scheduler.replan("job", "replan-ns", parsed, NOW) is None

    @pytest.mark.asyncio
    async def test_jobs_planned_before_upgrade_are_checked(self) -> None:
        # No planned-at (planned by an older controller): the first refresh is evaluated.
        scheduler, _ = _scheduler(_grid(SHARES_B, fetched_at=NOW))
        parsed = parse_annotations(
            _job(
                **{
                    ANNOTATION_PLANNED_START: (NOW + timedelta(hours=6)).isoformat(),
                    ANNOTATION_LATEST_START: (NOW + timedelta(hours=12)).isoformat(),
                }
            )
        )
        decision = await scheduler.replan("job", "replan-ns", parsed, NOW)
        assert decision is not None and decision.moved


class TestTimerReplan:
    """The release timer applies re-plans and posts a JobReplanned Event."""

    @pytest.fixture
    def events(self, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        captured: list[dict[str, Any]] = []

        async def fake_emit(**kwargs: Any) -> None:
            captured.append(kwargs)

        monkeypatch.setattr(handlers, "emit_event", fake_emit)
        return captured

    @staticmethod
    def _held_job(now: datetime, planned_in: timedelta) -> dict[str, Any]:
        annotations = _job(
            base=now,
            **{
                ANNOTATION_PLANNED_START: (now + planned_in).isoformat(),
                ANNOTATION_LATEST_START: (now + timedelta(hours=12)).isoformat(),
                ANNOTATION_PLANNED_AT: (now - timedelta(hours=1)).isoformat(),
            },
        )
        return {
            "annotations": annotations,
            "creationTimestamp": (now - timedelta(hours=1)).isoformat(),
        }

    @pytest.mark.asyncio
    async def test_job_replanned_event_and_patch(self, events: list[dict[str, Any]]) -> None:
        now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        # Green plateau +2h..+4h, the old plan at +6h is only 85 %.
        scheduler, _ = _scheduler(_grid(SHARES_B, fetched_at=datetime.now(UTC), base=now))
        set_scheduler(scheduler)
        meta = self._held_job(now, timedelta(hours=6))
        patch: dict[str, Any] = {}

        result = await handle_job_timer(
            spec={"suspend": True}, meta=meta, name="cron-run", namespace="replan-ns", patch=patch
        )

        assert result is None  # still held, just at a better time
        set_annotations = patch["metadata"]["annotations"]
        new_start = datetime.fromisoformat(set_annotations[ANNOTATION_PLANNED_START])
        assert now + timedelta(hours=2) <= new_start <= now + timedelta(hours=3)
        assert ANNOTATION_PLANNED_AT in set_annotations
        assert "suspend" not in patch.get("spec", {})

        replanned = [e for e in events if e["reason"] == "JobReplanned"]
        assert len(replanned) == 1
        message = replanned[0]["message"]
        assert (now + timedelta(hours=6)).isoformat() in message
        assert new_start.isoformat() in message
        assert "85.0% → 95.0%" in message

    @pytest.mark.asyncio
    async def test_replan_to_now_releases_on_same_tick(self, events: list[dict[str, Any]]) -> None:
        now = datetime.now(UTC)
        base = now.replace(minute=0, second=0, microsecond=0)
        # Right now is the greenest it will get.
        shares = [99, 99, 99, 40, 40, 40, 85, 85, 85, 40, 40, 40, 40, 40, 40, 40]
        scheduler, _ = _scheduler(_grid(shares, fetched_at=now, base=base))
        set_scheduler(scheduler)
        meta = self._held_job(now, timedelta(hours=6))
        patch: dict[str, Any] = {}

        result = await handle_job_timer(
            spec={"suspend": True}, meta=meta, name="now-run", namespace="replan-ns", patch=patch
        )

        assert result is not None and result["released"] and result["fallback"] is False
        assert patch["spec"]["suspend"] is False
        assert ANNOTATION_ACTUAL_START in patch["metadata"]["annotations"]
        assert [e["reason"] for e in events] == ["JobReplanned", "JobReleased"]
