"""Tests for per-job overrides, max-delay, live accounting wiring, health
endpoints, held-job tracking and the ``carbon-sim plan`` dry-run command."""

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner
from prometheus_client import REGISTRY

from carbon_scheduler.config import (
    ANNOTATION_ACTUAL_START,
    ANNOTATION_CO2_AVOIDED,
    ANNOTATION_DEADLINE,
    ANNOTATION_ESTIMATED_RUNTIME,
    ANNOTATION_EXPECTED_RENEWABLE_SHARE,
    ANNOTATION_FLEXIBLE,
    ANNOTATION_LATEST_START,
    ANNOTATION_MAX_DELAY,
    ANNOTATION_PLANNED_START,
    ANNOTATION_POLICY,
    ANNOTATION_RENEWABLE_THRESHOLD,
    Settings,
)
from carbon_scheduler.controller.annotations import AnnotationError, parse_annotations
from carbon_scheduler.controller.handlers import (
    handle_job_create,
    handle_job_timer,
    set_scheduler,
)
from carbon_scheduler.controller.scheduler import JobScheduler, ScheduleDecision
from carbon_scheduler.grid import GridData
from carbon_scheduler.grid.models import RenewableShareForecast
from carbon_scheduler.metrics.prometheus import (
    get_jobs_held_count,
    reset_jobs_held,
    set_ready,
    start_metrics_server,
)
from carbon_scheduler.planner.models import PolicyConfig
from carbon_scheduler.planner.policies import get_policy
from carbon_scheduler.sim.cli import main as sim_main
from carbon_scheduler.sim.plan import forecast_from_energy_charts, plan_job, sparkline

NOW = datetime(2026, 10, 29, 6, 0, tzinfo=UTC)

# Hourly forecast: low share early, a green peak at +6h..+8h, then low again
SHARES = [30, 30, 35, 40, 50, 60, 85, 90, 85, 50, 40, 35, 30, 30, 30, 30]


def _grid_data(shares: list[float] = SHARES, substitute: bool = False) -> GridData:
    forecast = RenewableShareForecast(
        unix_seconds=[int((NOW + timedelta(hours=i)).timestamp()) for i in range(len(shares))],
        ren_share=shares,
        substitute=substitute,
    )
    return GridData(forecast=forecast, fetched_at=NOW, country="de")


def _annotations(**extra: str) -> dict[str, str]:
    base = {
        ANNOTATION_FLEXIBLE: "true",
        ANNOTATION_DEADLINE: (NOW + timedelta(hours=14)).isoformat(),
        ANNOTATION_ESTIMATED_RUNTIME: "1h",
    }
    base.update(extra)
    return base


def _scheduler(**settings: Any) -> tuple[JobScheduler, MagicMock]:
    client = MagicMock()
    client.get_grid_data = AsyncMock(return_value=_grid_data())
    scheduler = JobScheduler(settings=Settings(**settings))
    scheduler.grid_client = client
    return scheduler, client


class TestOverrideAnnotations:
    def test_parses_overrides(self) -> None:
        parsed = parse_annotations(
            _annotations(
                **{
                    ANNOTATION_POLICY: "Threshold",
                    ANNOTATION_RENEWABLE_THRESHOLD: "75%",
                    ANNOTATION_MAX_DELAY: "4h",
                }
            )
        )
        assert parsed.policy_override == "threshold"
        assert parsed.renewable_threshold_override == 75
        assert parsed.max_delay == timedelta(hours=4)

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            (ANNOTATION_POLICY, "fastest"),
            (ANNOTATION_RENEWABLE_THRESHOLD, "abc"),
            (ANNOTATION_RENEWABLE_THRESHOLD, "150"),
            (ANNOTATION_MAX_DELAY, "soon"),
        ],
    )
    def test_invalid_overrides_raise(self, key: str, value: str) -> None:
        with pytest.raises(AnnotationError) as exc:
            parse_annotations(_annotations(**{key: value}))
        assert exc.value.annotation_key == key

    def test_threshold_override_does_not_leak_into_shared_config(self) -> None:
        shared = PolicyConfig(renewable_threshold_pct=60)
        get_policy("threshold", config=shared, threshold_pct=90)
        assert shared.renewable_threshold_pct == 60


class TestSchedulerFeatures:
    @pytest.mark.asyncio
    async def test_hold_writes_latest_start_share_and_co2(self) -> None:
        scheduler, _ = _scheduler(max_delay="")
        decision = await scheduler.calculate_schedule(
            "job", "ns", _annotations(), current_time=NOW
        )
        assert decision.should_release is False
        assert decision.planned_start == NOW + timedelta(hours=6)
        ann = decision.annotations_to_set
        assert ann[ANNOTATION_LATEST_START] == (
            NOW + timedelta(hours=14) - timedelta(hours=1) - timedelta(minutes=5)
        ).isoformat()
        assert float(ann[ANNOTATION_EXPECTED_RENEWABLE_SHARE]) == pytest.approx(87.5)
        assert float(ann[ANNOTATION_CO2_AVOIDED]) > 0

    @pytest.mark.asyncio
    async def test_policy_override_is_used(self) -> None:
        scheduler, _ = _scheduler(policy="greenest-window")
        decision = await scheduler.calculate_schedule(
            "job",
            "ns",
            _annotations(**{ANNOTATION_POLICY: "immediate"}),
            current_time=NOW,
        )
        assert decision.should_release is True
        assert decision.policy_used == "immediate"

    @pytest.mark.asyncio
    async def test_threshold_override_is_used(self) -> None:
        scheduler, _ = _scheduler(policy="threshold", renewable_threshold_pct=95)
        decision = await scheduler.calculate_schedule(
            "job",
            "ns",
            _annotations(**{ANNOTATION_RENEWABLE_THRESHOLD: "50"}),
            current_time=NOW,
        )
        # First slot with >= 50 % is +4h
        assert decision.planned_start == NOW + timedelta(hours=4)

    @pytest.mark.asyncio
    async def test_max_delay_annotation_caps_hold(self) -> None:
        scheduler, _ = _scheduler(max_delay="")
        decision = await scheduler.calculate_schedule(
            "job",
            "ns",
            _annotations(**{ANNOTATION_MAX_DELAY: "3h"}),
            current_time=NOW,
        )
        assert decision.planned_start is not None
        assert decision.planned_start <= NOW + timedelta(hours=3)
        assert decision.annotations_to_set.get(ANNOTATION_LATEST_START, "") <= (
            NOW + timedelta(hours=3)
        ).isoformat()

    @pytest.mark.asyncio
    async def test_global_max_delay_caps_hold(self) -> None:
        scheduler, _ = _scheduler(max_delay="2h")
        decision = await scheduler.calculate_schedule("job", "ns", _annotations(), current_time=NOW)
        assert decision.planned_start is None or decision.planned_start <= NOW + timedelta(hours=2)

    @pytest.mark.asyncio
    async def test_updates_grid_gauges(self) -> None:
        scheduler, _ = _scheduler()
        await scheduler.calculate_schedule("job", "ns", _annotations(), current_time=NOW)
        assert REGISTRY.get_sample_value("carbon_grid_renewable_share_percent") == 30.0
        assert REGISTRY.get_sample_value("carbon_forecast_age_seconds") == 0.0

    def test_release_at_recorded_latest_start(self) -> None:
        scheduler, _ = _scheduler()
        parsed = parse_annotations(
            _annotations(
                **{
                    ANNOTATION_PLANNED_START: (NOW + timedelta(hours=6)).isoformat(),
                    ANNOTATION_LATEST_START: (NOW + timedelta(hours=2)).isoformat(),
                }
            )
        )
        assert scheduler.should_release_now(parsed, NOW + timedelta(hours=1)) is False
        assert scheduler.should_release_now(parsed, NOW + timedelta(hours=2)) is True


class TestHandlerFeatures:
    @pytest.fixture(autouse=True)
    def _reset(self) -> None:
        reset_jobs_held("feat-ns")

    @pytest.mark.asyncio
    async def test_release_on_create_sets_actual_start(self) -> None:
        scheduler = MagicMock(spec=JobScheduler)
        scheduler.replan = AsyncMock(return_value=None)
        scheduler.calculate_schedule = AsyncMock(
            return_value=ScheduleDecision.release_now(policy_used="immediate")
        )
        set_scheduler(scheduler)
        patch: dict[str, Any] = {}
        await handle_job_create(
            spec={"suspend": True},
            meta={"annotations": _annotations()},
            name="j1",
            namespace="feat-ns",
            patch=patch,
        )
        assert ANNOTATION_ACTUAL_START in patch["metadata"]["annotations"]
        # Passes the job spec through for energy estimation
        assert scheduler.calculate_schedule.call_args.kwargs["job_spec"] == {"suspend": True}

    @pytest.mark.asyncio
    async def test_timer_tracks_held_jobs_and_records_co2_on_release(self) -> None:
        scheduler = MagicMock(spec=JobScheduler)
        scheduler.replan = AsyncMock(return_value=None)
        scheduler.should_release_now = MagicMock(return_value=False)
        set_scheduler(scheduler)

        planned = datetime.now(UTC) + timedelta(hours=1)
        meta = {
            "annotations": _annotations(
                **{
                    ANNOTATION_PLANNED_START: planned.isoformat(),
                    ANNOTATION_CO2_AVOIDED: "12.5",
                }
            )
        }

        # Two reconciles of the same held job count once (restart-safe gauge)
        for _ in range(2):
            await handle_job_timer(
                spec={"suspend": True}, meta=meta, name="held", namespace="feat-ns", patch={}
            )
        assert get_jobs_held_count("feat-ns") == 1.0

        scheduler.should_release_now.return_value = True
        meta["annotations"][ANNOTATION_PLANNED_START] = datetime.now(UTC).isoformat()
        result = await handle_job_timer(
            spec={"suspend": True}, meta=meta, name="held", namespace="feat-ns", patch={}
        )
        assert result is not None and result["fallback"] is False
        assert get_jobs_held_count("feat-ns") == 0.0
        assert (
            REGISTRY.get_sample_value(
                "carbon_co2_avoided_grams_total",
                {"namespace": "feat-ns", "job_name": "held"},
            )
            == 12.5
        )

    @pytest.mark.asyncio
    async def test_emit_event_without_operator_context_is_safe(self) -> None:
        from carbon_scheduler.controller.handlers import emit_event

        body = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "x"}}
        await emit_event("x", "ns", "JobHeld", "held", body=body)


class TestHealthServer:
    def test_healthz_readyz_and_metrics(self) -> None:
        server = start_metrics_server(port=0, addr="127.0.0.1")
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            set_ready(False)
            assert urllib.request.urlopen(f"{base}/healthz").read() == b"ok"
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(f"{base}/readyz")
            assert exc.value.code == 503

            set_ready(True)
            assert urllib.request.urlopen(f"{base}/readyz").status == 200
            assert b"carbon_jobs_held" in urllib.request.urlopen(f"{base}/metrics").read()
        finally:
            set_ready(False)
            server.shutdown()
            server.server_close()


class TestPlanCommand:
    @pytest.fixture
    def forecast_file(self, tmp_path: Path) -> Path:
        path = tmp_path / "forecast.json"
        path.write_text(json.dumps(_grid_data().forecast.model_dump()))  # type: ignore[union-attr]
        return path

    def test_plan_job_picks_green_peak(self) -> None:
        forecast = forecast_from_energy_charts(_grid_data().forecast.model_dump())  # type: ignore[union-attr]
        result = plan_job(
            forecast=forecast,
            now=NOW,
            runtime=timedelta(hours=1),
            deadline=NOW + timedelta(hours=14),
        )
        assert result.window.start == NOW + timedelta(hours=6)
        assert result.delay == timedelta(hours=6)
        assert result.avoided is not None and result.avoided.avoided_gco2 > 0

    def test_cli_plan(self, forecast_file: Path) -> None:
        result = CliRunner().invoke(
            sim_main,
            [
                "plan",
                "--runtime",
                "1h",
                "--within",
                "14h",
                "--forecast-file",
                str(forecast_file),
                "--now",
                NOW.isoformat(),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "greenest-window" in result.output
        assert "Est. CO₂ avoided" in result.output

    def test_cli_plan_requires_deadline(self, forecast_file: Path) -> None:
        result = CliRunner().invoke(
            sim_main, ["plan", "--runtime", "1h", "--forecast-file", str(forecast_file)]
        )
        assert result.exit_code != 0

    def test_sparkline(self) -> None:
        assert sparkline([0, 50, 100]) == "▁▅█"
        assert sparkline([]) == ""
