"""Tests for the balanced policy, relative deadlines (deadline-in) and release staggering."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from carbon_scheduler.config import (
    ANNOTATION_DEADLINE,
    ANNOTATION_DEADLINE_IN,
    ANNOTATION_DELAY_PENALTY,
    ANNOTATION_ESTIMATED_RUNTIME,
    ANNOTATION_FLEXIBLE,
    ANNOTATION_PLANNED_START,
    ANNOTATION_POLICY,
    Settings,
)
from carbon_scheduler.controller.annotations import AnnotationError, parse_annotations
from carbon_scheduler.controller.handlers import handle_job_timer, set_limiter, set_scheduler
from carbon_scheduler.controller.scheduler import JobScheduler
from carbon_scheduler.controller.staggering import ReleaseLimiter, is_deadline_critical
from carbon_scheduler.grid import GridData
from carbon_scheduler.grid.models import RenewableShareForecast
from carbon_scheduler.planner.models import Forecast, ForecastPoint, JobInfo, PolicyConfig
from carbon_scheduler.planner.policies import POLICY_NAMES, BalancedPolicy, get_policy
from carbon_scheduler.planner.window import select_window

NOW = datetime(2026, 10, 29, 6, 0, tzinfo=UTC)
# Small bump at +1h (60%), bigger peak at +10h (70%)
SHARES = [40, 60, 40, 40, 40, 40, 40, 40, 40, 40, 70, 40, 40, 40]


def _forecast(shares: list[float] = SHARES) -> Forecast:
    return Forecast(
        points=[
            ForecastPoint(timestamp=NOW + timedelta(hours=i), renewable_share_pct=s)
            for i, s in enumerate(shares)
        ]
    )


def _job(deadline_h: float = 13, runtime_m: int = 30) -> JobInfo:
    return JobInfo(
        name="j",
        submit_time=NOW,
        deadline=NOW + timedelta(hours=deadline_h),
        estimated_runtime=timedelta(minutes=runtime_m),
    )


class TestBalancedPolicy:
    def test_registered(self) -> None:
        assert "balanced" in POLICY_NAMES
        assert isinstance(get_policy("balanced"), BalancedPolicy)

    def test_zero_penalty_matches_greenest(self) -> None:
        cfg = PolicyConfig(delay_penalty_pct_per_hour=0)
        balanced = get_policy("balanced", config=cfg).select(_job(), _forecast(), NOW)
        greenest = get_policy("greenest-window", config=cfg).select(_job(), _forecast(), NOW)
        assert balanced.start == greenest.start == NOW + timedelta(hours=10)

    def test_penalty_prefers_earlier_window(self) -> None:
        # Peak is 10 points better but 9h later: penalty 2/h makes the +1h bump win
        window = get_policy("balanced", config=PolicyConfig(delay_penalty_pct_per_hour=2)).select(
            _job(), _forecast(), NOW
        )
        assert window.start == NOW + timedelta(hours=1)
        assert not window.is_fallback

    def test_penalty_override_via_factory(self) -> None:
        policy = get_policy("balanced", delay_penalty_pct_per_hour=0.5)
        assert policy.config.delay_penalty_pct_per_hour == 0.5
        assert policy.select(_job(), _forecast(), NOW).start == NOW + timedelta(hours=10)

    def test_respects_deadline(self) -> None:
        window = get_policy("balanced").select(_job(deadline_h=5), _forecast(), NOW)
        assert window.end <= NOW + timedelta(hours=5) - timedelta(minutes=5)

    @pytest.mark.parametrize(
        ("forecast", "reason"),
        [(None, "no_forecast_data"), (Forecast(points=[], degraded=False), "no_forecast_data")],
    )
    def test_fallbacks(self, forecast: Forecast | None, reason: str) -> None:
        window = get_policy("balanced").select(_job(), forecast, NOW)
        assert window.is_fallback and window.fallback_reason == reason

    def test_degraded_fallback(self) -> None:
        fc = _forecast()
        fc.degraded = True
        assert get_policy("balanced").select(_job(), fc, NOW).fallback_reason == (
            "forecast_data_degraded"
        )

    def test_select_window_dispatch(self) -> None:
        window = select_window(_job(), _forecast(), NOW, "balanced", PolicyConfig())
        assert window.policy_used == "balanced"
        assert window.start == NOW + timedelta(hours=1)


BASE = {ANNOTATION_FLEXIBLE: "true", ANNOTATION_ESTIMATED_RUNTIME: "1h"}


class TestRelativeDeadline:
    def test_deadline_in_resolved_from_submit_time(self) -> None:
        parsed = parse_annotations({**BASE, ANNOTATION_DEADLINE_IN: "6h"}, submit_time=NOW)
        assert parsed.deadline == NOW + timedelta(hours=6)
        assert parsed.deadline_is_relative

    def test_absolute_deadline_wins(self) -> None:
        parsed = parse_annotations(
            {**BASE, ANNOTATION_DEADLINE: "2026-10-29T09:00:00Z", ANNOTATION_DEADLINE_IN: "6h"},
            submit_time=NOW,
        )
        assert parsed.deadline == NOW + timedelta(hours=3)
        assert not parsed.deadline_is_relative

    def test_missing_both_raises(self) -> None:
        with pytest.raises(AnnotationError):
            parse_annotations(BASE)

    def test_invalid_deadline_in(self) -> None:
        with pytest.raises(AnnotationError) as e:
            parse_annotations({**BASE, ANNOTATION_DEADLINE_IN: "soon"})
        assert e.value.annotation_key == ANNOTATION_DEADLINE_IN

    def test_delay_penalty_annotation(self) -> None:
        parsed = parse_annotations(
            {**BASE, ANNOTATION_DEADLINE_IN: "6h", ANNOTATION_DELAY_PENALTY: "1.5"}
        )
        assert parsed.delay_penalty_override == 1.5
        with pytest.raises(AnnotationError):
            parse_annotations({**BASE, ANNOTATION_DEADLINE_IN: "6h", ANNOTATION_DELAY_PENALTY: "-1"})
        with pytest.raises(AnnotationError):
            parse_annotations({**BASE, ANNOTATION_DEADLINE_IN: "6h", ANNOTATION_DELAY_PENALTY: "x"})

    def test_balanced_policy_annotation_accepted(self) -> None:
        parsed = parse_annotations(
            {**BASE, ANNOTATION_DEADLINE_IN: "6h", ANNOTATION_POLICY: "balanced"}
        )
        assert parsed.policy_override == "balanced"

    async def test_scheduler_pins_resolved_deadline(self) -> None:
        shares = [30, 30, 30, 90, 90, 30, 30, 30, 30, 30]
        grid = GridData(
            forecast=RenewableShareForecast(
                unix_seconds=[int((NOW + timedelta(hours=i)).timestamp()) for i in range(10)],
                ren_share=shares,
                substitute=False,
            ),
            fetched_at=NOW,
            country="de",
        )
        client = MagicMock()
        client.get_grid_data = AsyncMock(return_value=grid)
        scheduler = JobScheduler(grid_client=client, settings=Settings(max_delay=""))
        decision = await scheduler.calculate_schedule(
            "j", "ns", {**BASE, ANNOTATION_DEADLINE_IN: "8h"}, current_time=NOW, submit_time=NOW
        )
        assert not decision.should_release
        assert decision.annotations_to_set[ANNOTATION_DEADLINE] == (
            NOW + timedelta(hours=8)
        ).isoformat()
        assert decision.planned_start == NOW + timedelta(hours=3)

    async def test_scheduler_does_not_touch_absolute_deadline(self) -> None:
        scheduler = JobScheduler(grid_client=None, settings=Settings())
        decision = await scheduler.calculate_schedule(
            "j", "ns", {**BASE, ANNOTATION_DEADLINE: "2026-10-29T12:00:00Z"}, current_time=NOW
        )
        assert ANNOTATION_DEADLINE not in decision.annotations_to_set


class TestReleaseLimiter:
    def test_disabled_always_grants(self) -> None:
        limiter = ReleaseLimiter(0)
        assert all(limiter.try_acquire(NOW) for _ in range(100))
        assert not limiter.enabled

    def test_caps_per_minute_and_recovers(self) -> None:
        limiter = ReleaseLimiter(2)
        assert limiter.try_acquire(NOW)
        assert limiter.try_acquire(NOW + timedelta(seconds=10))
        assert not limiter.try_acquire(NOW + timedelta(seconds=20))
        assert limiter.deferred_total == 1
        assert limiter.in_flight(NOW + timedelta(seconds=20)) == 2
        assert limiter.try_acquire(NOW + timedelta(seconds=61))

    def test_critical_bypasses(self) -> None:
        limiter = ReleaseLimiter(1)
        assert limiter.try_acquire(NOW)
        assert limiter.try_acquire(NOW, critical=True)

    def test_is_deadline_critical(self) -> None:
        interval = timedelta(seconds=60)
        assert not is_deadline_critical(NOW, None, interval)
        assert not is_deadline_critical(NOW, NOW + timedelta(minutes=10), interval)
        assert is_deadline_critical(NOW, NOW + timedelta(seconds=90), interval)

    async def test_timer_defers_when_limited(self) -> None:
        set_scheduler(JobScheduler(grid_client=None, settings=Settings()))
        limiter = ReleaseLimiter(1)
        limiter.try_acquire(datetime.now(UTC))  # use up the budget
        set_limiter(limiter)
        try:
            now = datetime.now(UTC)
            annotations = {
                **BASE,
                ANNOTATION_DEADLINE: (now + timedelta(hours=10)).isoformat(),
                ANNOTATION_PLANNED_START: (now - timedelta(seconds=5)).isoformat(),
            }
            patch: dict = {}
            result = await handle_job_timer(
                spec={"suspend": True},
                meta={"annotations": annotations},
                name="deferred",
                namespace="ns",
                patch=patch,
            )
            assert result is None
            assert "spec" not in patch
            assert limiter.deferred_total == 1
        finally:
            set_limiter(None)
