"""Tests for the kubectl carbon plugin."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from carbon_scheduler import kubectl_plugin as kp
from carbon_scheduler.config import (
    ANNOTATION_CO2_AVOIDED,
    ANNOTATION_EXPECTED_RENEWABLE_SHARE,
    ANNOTATION_FALLBACK_REASON,
    ANNOTATION_FLEXIBLE,
    ANNOTATION_PLANNED_START,
    ANNOTATION_POLICY_USED,
)

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _job(name: str, suspend: bool, **ann: str) -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "namespace": "data",
            "annotations": {ANNOTATION_FLEXIBLE: "true", **ann},
        },
        "spec": {"suspend": suspend},
        "status": {},
    }


HELD = _job(
    "etl",
    True,
    **{
        ANNOTATION_PLANNED_START: (NOW + timedelta(hours=2, minutes=30)).isoformat(),
        ANNOTATION_EXPECTED_RENEWABLE_SHARE: "78.4",
        ANNOTATION_CO2_AVOIDED: "120.5",
        ANNOTATION_POLICY_USED: "greenest-window",
    },
)


class TestRows:
    def test_held_row(self) -> None:
        row = kp.job_row(HELD, now=NOW)
        assert row["state"] == "Held"
        assert row["starts_in"] == "2h30m"
        assert row["share"] == "78%"
        assert row["co2"] == "120 g" or row["co2"] == "121 g"

    @pytest.mark.parametrize(
        ("job", "state"),
        [
            (_job("a", True), "Pending"),
            (_job("b", False, **{ANNOTATION_FALLBACK_REASON: "x"}), "Fallback"),
            (_job("c", False), "Released"),
            ({**_job("d", False), "status": {"active": 1}}, "Running"),
            (
                {**_job("e", False), "status": {"conditions": [{"type": "Complete", "status": "True"}]}},
                "Complete",
            ),
        ],
    )
    def test_states(self, job: dict[str, Any], state: str) -> None:
        assert kp.job_state(job) == state

    def test_flexible_filter_and_humanize(self) -> None:
        assert kp.is_flexible(HELD)
        assert not kp.is_flexible({"metadata": {"annotations": {}}})
        assert kp._humanize(-30) == "-30s"
        assert kp._humanize(600) == "10m"
        assert kp._parse_dt("garbage") is None

    def test_release_patch(self) -> None:
        patch = kp.release_patch(NOW)
        assert patch["spec"]["suspend"] is False
        assert patch["metadata"]["annotations"][ANNOTATION_FALLBACK_REASON] == "manual_release"


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    api = MagicMock()
    api.api_client.sanitize_for_serialization.side_effect = lambda obj: obj
    api.list_job_for_all_namespaces.return_value = MagicMock(
        items=[HELD, {"metadata": {"name": "plain", "annotations": {}}}]
    )
    api.list_namespaced_job.return_value = MagicMock(items=[HELD])
    api.read_namespaced_job.return_value = HELD
    monkeypatch.setattr(kp, "_batch_api", lambda: api)
    monkeypatch.setattr(kp, "_current_namespace", lambda: "data")
    return api


@pytest.mark.usefixtures("fake_api")
def test_status_all_namespaces() -> None:
    result = CliRunner().invoke(kp.main, ["status", "-A"])
    assert result.exit_code == 0, result.output
    assert "etl" in result.output and "plain" not in result.output
    assert "1 held" in result.output


def test_status_empty(fake_api: MagicMock) -> None:
    fake_api.list_namespaced_job.return_value = MagicMock(items=[])
    result = CliRunner().invoke(kp.main, ["status"])
    assert "No flexible Jobs" in result.output


@pytest.mark.usefixtures("fake_api")
def test_explain() -> None:
    result = CliRunner().invoke(kp.main, ["explain", "etl"])
    assert result.exit_code == 0, result.output
    assert "planned-start" in result.output and "greenest-window" in result.output


def test_release(fake_api: MagicMock) -> None:
    result = CliRunner().invoke(kp.main, ["release", "etl", "--yes"])
    assert result.exit_code == 0, result.output
    name, ns, body = fake_api.patch_namespaced_job.call_args.args
    assert (name, ns) == ("etl", "data")
    assert body["spec"]["suspend"] is False


def test_release_aborts_without_confirmation(fake_api: MagicMock) -> None:
    result = CliRunner().invoke(kp.main, ["release", "etl"], input="n\n")
    assert result.exit_code != 0
    fake_api.patch_namespaced_job.assert_not_called()
