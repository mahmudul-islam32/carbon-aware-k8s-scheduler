"""Tests for the kind end-to-end harness (F-33).

The cluster run itself happens in CI (``scripts/e2e-kind.sh``). These tests pin
the pieces it relies on: the mock Energy-Charts API, the controller's behaviour
against it, and the wiring of the script, manifests and CI job.
"""

import json
import subprocess
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from carbon_scheduler.config import (
    ANNOTATION_LATEST_START,
    ANNOTATION_PLANNED_START,
    Settings,
)
from carbon_scheduler.controller.annotations import parse_annotations
from carbon_scheduler.controller.scheduler import JobScheduler
from carbon_scheduler.grid import EnergyChartsClient
from carbon_scheduler.sim.cli import main as sim_cli
from carbon_scheduler.sim.mock_api import MockEnergyChartsServer, MockProfile

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "e2e-kind.sh"
E2E_DIR = ROOT / "tests" / "e2e"
T0 = 1_800_000_000.0  # fixed "now" for deterministic profile tests


def _get(url: str) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _e2e_job() -> dict[str, Any]:
    return yaml.safe_load((E2E_DIR / "job.yaml").read_text())


class TestMockProfile:
    def test_green_start_is_anchored_on_first_request(self) -> None:
        profile = MockProfile(green_after=90)
        assert profile.green_start(T0) == T0 + 90
        # Later requests (cache refresh, re-plan) do not move the window.
        assert profile.green_start(T0 + 600) == T0 + 90

    def test_forecast_steps_from_low_to_high(self) -> None:
        profile = MockProfile(green_after=90, low_share=20, high_share=90, step=60)
        data = profile.forecast(T0)
        assert data["substitute"] is False
        for ts, share in zip(data["unix_seconds"], data["ren_share"], strict=True):
            assert share == (90 if ts >= T0 + 90 else 20)
        assert all(ts % 60 == 0 for ts in data["unix_seconds"])
        assert data["unix_seconds"][0] <= T0 - 3600 + 60
        assert data["unix_seconds"][-1] >= T0 + 24 * 3600 - 60

    def test_signal_and_power_follow_the_same_step(self) -> None:
        profile = MockProfile(green_after=90)
        signal = profile.signal(T0)
        power = profile.public_power(T0)
        assert set(signal["signal"]) == {0, 2}
        assert {"unix_seconds", "Solar", "Wind onshore", "Fossil gas"} <= set(power)
        assert len(power["Solar"]) == len(power["unix_seconds"])

    def test_substitute_flag_is_passed_through(self) -> None:
        profile = MockProfile(substitute=True)
        assert profile.forecast(T0)["substitute"] is True
        assert profile.signal(T0)["substitute"] is True


class TestMockServer:
    def test_serves_energy_charts_endpoints(self) -> None:
        with MockEnergyChartsServer(clock=lambda: T0) as server:
            status, health = _get(f"{server.url}/healthz")
            assert (status, health) == (200, {"status": "ok"})
            for path in ("/ren_share_forecast", "/signal", "/public_power"):
                status, body = _get(f"{server.url}{path}?country=de")
                assert status == 200
                assert body["unix_seconds"]
            status, body = _get(f"{server.url}/nope")
            assert status == 404

    def test_cli_help_lists_mock_api(self) -> None:
        result = CliRunner().invoke(sim_cli, ["mock-api", "--help"])
        assert result.exit_code == 0
        assert "--green-after" in result.output


class TestControllerAgainstMock:
    """Same flow the kind test asserts, minus the cluster: hold, then release."""

    async def test_job_is_held_until_green_and_then_released(self) -> None:
        now = datetime.now(UTC)
        profile = MockProfile(green_after=90, step=60)
        with MockEnergyChartsServer(profile) as server:
            async with EnergyChartsClient(base_url=server.url, country="de") as client:
                scheduler = JobScheduler(grid_client=client, settings=Settings())
                job = _e2e_job()
                annotations = job["metadata"]["annotations"]
                decision = await scheduler.calculate_schedule(
                    "e2e-flexible",
                    "e2e",
                    annotations,
                    current_time=now,
                    job_spec=job["spec"],
                    submit_time=now,
                )

        assert profile.anchor is not None
        green_start = datetime.fromtimestamp(profile.anchor + 90, tz=UTC)
        assert not decision.should_release
        assert not decision.is_fallback
        assert decision.planned_start is not None
        assert green_start <= decision.planned_start <= green_start + timedelta(seconds=60)
        assert ANNOTATION_PLANNED_START in decision.annotations_to_set
        assert ANNOTATION_LATEST_START in decision.annotations_to_set

        parsed = parse_annotations(
            {**annotations, **decision.annotations_to_set}, submit_time=now
        )
        assert not scheduler.should_release_now(parsed, now)
        assert scheduler.should_release_now(parsed, decision.planned_start)

    async def test_degraded_mock_data_fails_open(self) -> None:
        now = datetime.now(UTC)
        with MockEnergyChartsServer(MockProfile(substitute=True)) as server:
            async with EnergyChartsClient(base_url=server.url, country="de") as client:
                scheduler = JobScheduler(grid_client=client, settings=Settings())
                decision = await scheduler.calculate_schedule(
                    "e2e-flexible",
                    "e2e",
                    _e2e_job()["metadata"]["annotations"],
                    current_time=now,
                    submit_time=now,
                )
        assert decision.should_release
        assert decision.is_fallback


class TestE2EWiring:
    def test_script_is_executable_valid_bash(self) -> None:
        assert SCRIPT.stat().st_mode & 0o111
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True)

    def test_script_covers_every_acceptance_step(self) -> None:
        text = SCRIPT.read_text()
        # CI job: kind cluster, image build, helm install, /readyz
        for needle in ("kind create cluster", "docker build", "helm install", "/readyz"):
            assert needle in text
        # Mocked forecast endpoint, planned-start written, Job released
        assert "mock-energy-charts" in text
        assert "config.gridApiBaseUrl" in text
        assert "has_annotation planned-start" in text
        assert "is_released" in text
        # kubectl describe shows the JobHeld Event
        assert "describe_has JobHeld" in text
        assert "kubectl -n \"$JOB_NAMESPACE\" describe job" in text

    def test_ci_runs_the_script(self) -> None:
        workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
        job = workflow["jobs"]["e2e"]
        runs = [step.get("run", "") for step in job["steps"]]
        assert "./scripts/e2e-kind.sh" in runs
        uses = [step.get("uses", "") for step in job["steps"]]
        assert any(u.startswith("helm/kind-action") for u in uses)
        assert any(u.startswith("azure/setup-helm") for u in uses)

    def test_mock_manifest_runs_the_cli_from_the_controller_image(self) -> None:
        docs = list(yaml.safe_load_all((E2E_DIR / "mock-energy-charts.yaml").read_text()))
        kinds = {d["kind"]: d for d in docs}
        assert set(kinds) == {"Deployment", "Service"}
        container = kinds["Deployment"]["spec"]["template"]["spec"]["containers"][0]
        assert container["image"] == "IMAGE_PLACEHOLDER"
        assert container["command"][:2] == ["carbon-sim", "mock-api"]
        assert kinds["Service"]["spec"]["ports"][0]["port"] == 8080

    def test_e2e_job_is_a_suspended_flexible_job(self) -> None:
        job = _e2e_job()
        assert job["spec"]["suspend"] is True
        parsed = parse_annotations(job["metadata"]["annotations"], submit_time=datetime.now(UTC))
        assert parsed.estimated_runtime == timedelta(minutes=5)


@pytest.mark.parametrize("path", ["/ren_share_forecast", "/signal"])
def test_routes_are_registered(path: str) -> None:
    from carbon_scheduler.sim.mock_api import ROUTES

    assert path in ROUTES
