"""Tests for the dashboard state, HTTP routes, demo cluster and synthetic data."""

import json
import urllib.request
from datetime import UTC, datetime, timedelta

from click.testing import CliRunner

from carbon_scheduler.config import (
    ANNOTATION_CO2_AVOIDED,
    ANNOTATION_DEADLINE,
    ANNOTATION_ESTIMATED_RUNTIME,
    ANNOTATION_EXPECTED_RENEWABLE_SHARE,
    ANNOTATION_PLANNED_START,
    ANNOTATION_POLICY_USED,
)
from carbon_scheduler.dashboard import STATE, DashboardState, JobRecord
from carbon_scheduler.dashboard.demo import TEMPLATES, DemoCluster
from carbon_scheduler.dashboard.records import record_from_annotations
from carbon_scheduler.dashboard.web import dashboard_html
from carbon_scheduler.metrics.prometheus import start_metrics_server
from carbon_scheduler.sim.synthetic import synthetic_forecast, synthetic_shares

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _state() -> DashboardState:
    s = DashboardState()
    s.clock = lambda: NOW
    return s


class TestSynthetic:
    def test_deterministic_and_bounded(self) -> None:
        a = synthetic_shares(NOW, hours=72, seed=1)
        b = synthetic_shares(NOW, hours=72, seed=1)
        assert a == b
        assert len(a) == 72 * 4 + 1
        assert all(3 <= v <= 97 for _, v in a)
        assert a[1][0] - a[0][0] == timedelta(minutes=15)

    def test_has_daily_solar_variation(self) -> None:
        shares = [v for _, v in synthetic_shares(NOW.replace(month=6), hours=24 * 7, seed=3)]
        assert max(shares) - min(shares) > 20

    def test_naive_start_is_utc(self) -> None:
        fc = synthetic_forecast(datetime(2026, 1, 1), hours=2)
        assert fc.points[0].timestamp.tzinfo is not None


class TestDashboardState:
    def test_snapshot_totals_and_co2(self) -> None:
        s = _state()
        s.set_forecast(synthetic_forecast(NOW - timedelta(hours=2), hours=40))
        s.upsert_job(JobRecord("ns", "a", "held", submitted=NOW - timedelta(hours=2),
                               planned_start=NOW, co2_avoided_g=50.0, team="t1"))
        s.upsert_job(JobRecord("ns", "b", "held", co2_avoided_g=10.0))
        s.mark_released("ns", "a", at=NOW)
        s.mark_released("ns", "c", fallback=True, reason="no data")
        s.add_event("ns", "a", "JobReleased", "ok")
        snap = s.snapshot()
        t = snap["totals"]
        assert (t["held"], t["released"], t["fallback"]) == (1, 1, 1)
        assert t["co2_avoided_g"] == 50.0
        assert t["co2_planned_g"] == 10.0
        assert t["mean_delay_seconds"] == 7200
        assert t["share_now"] is not None and t["share_peak_ahead"] is not None
        assert snap["co2_series"][0][1] == 50.0
        assert snap["events"][0]["reason"] == "JobReleased"
        json.dumps(snap)  # serialisable

    def test_upsert_keeps_existing_fields(self) -> None:
        s = _state()
        s.upsert_job(JobRecord("ns", "a", "held", co2_avoided_g=5.0, team="x"))
        s.upsert_job(JobRecord("ns", "a", "held", policy="balanced"))
        job = s.snapshot()["jobs"][0]
        assert job["co2_avoided_g"] == 5.0 and job["policy"] == "balanced" and job["team"] == "x"

    def test_fallback_release_adds_no_co2(self) -> None:
        s = _state()
        s.upsert_job(JobRecord("ns", "a", "held", co2_avoided_g=5.0))
        s.mark_released("ns", "a", fallback=True)
        assert s.snapshot()["totals"]["co2_avoided_g"] == 0

    def test_reset(self) -> None:
        s = _state()
        s.upsert_job(JobRecord("ns", "a", "held"))
        s.reset()
        assert s.snapshot()["jobs"] == []

    def test_current_share_without_forecast(self) -> None:
        assert _state().current_share() is None


def test_record_from_annotations() -> None:
    rec = record_from_annotations(
        "ns",
        "job",
        {
            ANNOTATION_DEADLINE: "2026-11-01T20:00:00Z",
            ANNOTATION_ESTIMATED_RUNTIME: "1h30m",
            ANNOTATION_PLANNED_START: "2026-11-01T14:00:00Z",
            ANNOTATION_EXPECTED_RENEWABLE_SHARE: "71.5",
            ANNOTATION_CO2_AVOIDED: "bad",
            ANNOTATION_POLICY_USED: "balanced",
        },
        "held",
        labels={"team": "data"},
    )
    assert rec.runtime_seconds == 5400
    assert rec.expected_share_pct == 71.5
    assert rec.co2_avoided_g is None
    assert rec.team == "data"
    assert rec.planned_start == datetime(2026, 11, 1, 14, tzinfo=UTC)


class TestDemoCluster:
    def test_warmup_produces_jobs_and_events(self) -> None:
        state = DashboardState()
        demo = DemoCluster(seed=3, state=state, start=NOW)
        snap = state.snapshot()
        assert snap["mode"] == "demo"
        assert len(snap["jobs"]) > 5
        assert snap["events"]
        assert demo.now() == NOW
        # every held job respects its deadline
        for job in snap["jobs"]:
            if job["planned_start"] and job["deadline"]:
                end = datetime.fromisoformat(job["planned_start"]) + timedelta(
                    seconds=job["runtime_seconds"]
                )
                assert end <= datetime.fromisoformat(job["deadline"])

    def test_advance_releases_and_extends_forecast(self) -> None:
        state = DashboardState()
        demo = DemoCluster(seed=5, state=state, start=NOW)
        demo.advance_to(NOW + timedelta(days=9))
        snap = state.snapshot()
        assert snap["totals"]["released"] > 20
        assert snap["totals"]["co2_avoided_g"] > 0
        assert demo.forecast.points[-1].timestamp > NOW + timedelta(days=10)

    def test_submit_specific_template(self) -> None:
        state = DashboardState()
        demo = DemoCluster(seed=1, state=state, start=NOW, warmup=timedelta(0))
        rec = demo.submit(TEMPLATES[0])
        assert rec.name.startswith(TEMPLATES[0].name)

    def test_background_thread_starts_and_stops(self) -> None:
        demo = DemoCluster(seed=1, state=DashboardState(), start=NOW, speed=60)
        demo.start()
        demo.start()  # idempotent
        demo.stop()


def test_http_routes() -> None:
    STATE.reset()
    STATE.upsert_job(JobRecord("ns", "web", "held"))
    server = start_metrics_server(0, addr="127.0.0.1")
    port = server.server_port
    try:
        html = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read()
        assert b"Carbon-Aware Scheduler" in html
        assert html == dashboard_html()
        data = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state").read())
        assert data["jobs"][0]["name"] == "web"
        metrics = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics").read()
        assert b"carbon_jobs_held" in metrics
    finally:
        server.shutdown()
        STATE.reset()


def test_metrics_only_when_dashboard_disabled() -> None:
    server = start_metrics_server(0, addr="127.0.0.1", dashboard=False)
    try:
        body = urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/").read()
        assert b"# HELP" in body
    finally:
        server.shutdown()


def test_dashboard_cli_runs_briefly() -> None:
    from carbon_scheduler.sim.cli import main

    result = CliRunner().invoke(main, ["dashboard", "--port", "0", "--duration", "0.3"])
    assert result.exit_code == 0, result.output
    assert "Dashboard running" in result.output
    STATE.reset()
