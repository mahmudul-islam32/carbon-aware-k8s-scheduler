"""Tests for F-32: the PrometheusRule alert pack and the hold-limit metric behind it."""

import os
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from prometheus_client import REGISTRY

from carbon_scheduler.config import (
    ANNOTATION_DEADLINE,
    ANNOTATION_ESTIMATED_RUNTIME,
    ANNOTATION_FLEXIBLE,
    ANNOTATION_MAX_DELAY,
    ANNOTATION_PLANNED_START,
    Settings,
)
from carbon_scheduler.controller.handlers import (
    handle_job_create,
    handle_job_delete,
    handle_job_timer,
    set_scheduler,
)
from carbon_scheduler.controller.scheduler import JobScheduler, ScheduleDecision
from carbon_scheduler.metrics.prometheus import (
    get_jobs_held_count,
    reset_jobs_held,
    set_hold_limit,
    track_held_job,
    untrack_held_job,
)

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "charts" / "carbon-scheduler"
TEMPLATE = CHART / "templates" / "prometheusrule.yaml"
RUNBOOKS = ROOT / "docs" / "RUNBOOKS.md"
PROMTOOL_TEST = ROOT / "tests" / "alerts" / "prometheusrule-test.yaml"
ALERTS = (
    "CarbonSchedulerForecastStale",
    "CarbonSchedulerHighFallbackRate",
    "CarbonSchedulerEnergyChartsErrors",
    "CarbonSchedulerJobHeldTooLong",
)
NS = "f32-ns"
HOLD_LIMIT = "carbon_job_hold_limit_timestamp_seconds"


def _hold_limit(job_name: str, namespace: str = NS) -> float | None:
    return REGISTRY.get_sample_value(HOLD_LIMIT, {"namespace": namespace, "job_name": job_name})


def _github_anchor(heading: str) -> str:
    """GitHub's heading slug: lower case, punctuation dropped, spaces to dashes."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def _tool(name: str) -> str | None:
    return os.environ.get(f"CARBON_TEST_{name.upper()}") or shutil.which(name)


class TestChartValues:
    def test_alerts_are_opt_in(self) -> None:
        values = yaml.safe_load((CHART / "values.yaml").read_text())
        assert values["alerts"]["enabled"] is False

    def test_defaults_match_the_spec(self) -> None:
        alerts = yaml.safe_load((CHART / "values.yaml").read_text())["alerts"]
        assert alerts["forecastStale"]["maxAgeSeconds"] == 2 * 3600
        assert alerts["fallbackRate"]["threshold"] == 0.2
        assert alerts["fallbackRate"]["window"] == "1h"
        assert alerts["runbookBaseUrl"].endswith("docs/RUNBOOKS.md")


class TestTemplate:
    def test_defines_every_alert(self) -> None:
        text = TEMPLATE.read_text()
        for alert in ALERTS:
            assert f"alert: {alert}" in text

    def test_uses_only_exported_metrics(self) -> None:
        text = TEMPLATE.read_text()
        referenced = set(re.findall(r'\(list \. "([a-z_]+)"', text))
        assert referenced == {
            "carbon_forecast_age_seconds",
            "fallback_releases_total",
            "carbon_jobs_released_total",
            "carbon_api_requests_total",
            HOLD_LIMIT,
        }
        exported = {m.name for m in REGISTRY.collect()}
        # Counters are collected without their _total suffix
        exported |= {f"{name}_total" for name in exported}
        assert referenced <= exported


class TestRunbooks:
    def test_each_alert_has_a_runbook_section(self) -> None:
        headings = {
            _github_anchor(line[4:])
            for line in RUNBOOKS.read_text().splitlines()
            if line.startswith("### ")
        }
        template = TEMPLATE.read_text()
        for alert in ALERTS:
            anchor = alert.lower()
            assert anchor in headings, f"no runbook section for {alert}"
            assert f"#{anchor}" in template, f"{alert} does not link its runbook"

    def test_runbook_paragraphs_are_not_empty(self) -> None:
        text = RUNBOOKS.read_text()
        for alert in ALERTS:
            section = text.split(f"### {alert}", 1)[1].split("\n### ", 1)[0]
            assert "**What it means.**" in section
            assert "**Check.**" in section
            assert "**Fix.**" in section


@pytest.mark.skipif(
    not (_tool("helm") and _tool("promtool")),
    reason="helm and promtool are needed to render and unit-test the rules",
)
class TestRenderedRules:
    @staticmethod
    def _render(*args: str) -> str:
        helm = _tool("helm")
        assert helm is not None
        return subprocess.run(
            [helm, "template", "t", str(CHART), "--kube-version", "1.29.0", *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def test_disabled_by_default(self) -> None:
        assert "kind: PrometheusRule" not in self._render()

    def test_promtool_check_and_unit_tests(self) -> None:
        promtool = _tool("promtool")
        assert promtool is not None
        docs = [
            d
            for d in yaml.safe_load_all(self._render("--set", "alerts.enabled=true"))
            if d and d.get("kind") == "PrometheusRule"
        ]
        assert len(docs) == 1
        rules_file = PROMTOOL_TEST.parent / "rendered-rules.yaml"
        rules_file.write_text(yaml.safe_dump(docs[0]["spec"]))

        subprocess.run([promtool, "check", "rules", str(rules_file)], check=True)
        subprocess.run([promtool, "test", "rules", str(PROMTOOL_TEST)], check=True)

    def test_selector_is_added_to_every_metric(self) -> None:
        rendered = self._render(
            "--set", "alerts.enabled=true", "--set-string", 'alerts.selector=job="x"'
        )
        spec = next(d for d in yaml.safe_load_all(rendered) if d and d["kind"] == "PrometheusRule")
        for rule in spec["spec"]["groups"][0]["rules"]:
            metrics = re.findall(r"\b(carbon_[a-z_]+|fallback_releases_total)\b(\{[^}]*\})?", rule["expr"])
            assert metrics
            assert all('job="x"' in matchers for _, matchers in metrics), rule["alert"]


class TestHoldLimitMetric:
    @pytest.fixture(autouse=True)
    def _reset(self) -> None:
        reset_jobs_held(NS)

    def test_set_and_clear(self) -> None:
        set_hold_limit(NS, "a", 1_000.0)
        assert _hold_limit("a") == 1_000.0
        set_hold_limit(NS, "a", None)
        assert _hold_limit("a") is None
        set_hold_limit(NS, "a", None)  # clearing twice is harmless

    def test_untrack_clears_the_series(self) -> None:
        track_held_job(NS, "b")
        set_hold_limit(NS, "b", 1_000.0)
        assert untrack_held_job(NS, "b") is True
        assert _hold_limit("b") is None

    def test_reset_clears_the_series(self) -> None:
        track_held_job(NS, "c")
        set_hold_limit(NS, "c", 1_000.0)
        reset_jobs_held(NS)
        assert _hold_limit("c") is None


class TestHandlersExposeHoldLimit:
    CREATED = datetime.now(UTC).replace(microsecond=0)

    @pytest.fixture(autouse=True)
    def _reset(self) -> None:
        reset_jobs_held(NS)

    def _meta(self, **extra: str) -> dict[str, Any]:
        annotations = {
            ANNOTATION_FLEXIBLE: "true",
            ANNOTATION_DEADLINE: (self.CREATED + timedelta(hours=20)).isoformat(),
            ANNOTATION_ESTIMATED_RUNTIME: "1h",
        }
        annotations.update(extra)
        return {"annotations": annotations, "creationTimestamp": self.CREATED.isoformat()}

    @staticmethod
    def _holding_scheduler(max_delay: str = "24h") -> JobScheduler:
        scheduler = JobScheduler(settings=Settings(max_delay=max_delay))
        hold = ScheduleDecision.hold_until(
            planned_start=datetime.now(UTC) + timedelta(hours=2), policy_used="greenest-window"
        )
        scheduler.calculate_schedule = AsyncMock(return_value=hold)  # type: ignore[method-assign]
        return scheduler

    async def test_held_job_gets_creation_plus_global_max_delay(self) -> None:
        set_scheduler(self._holding_scheduler("24h"))
        await handle_job_create(
            spec={"suspend": True}, meta=self._meta(), name="held", namespace=NS, patch={}
        )
        assert _hold_limit("held") == (self.CREATED + timedelta(hours=24)).timestamp()

    async def test_per_job_max_delay_wins(self) -> None:
        set_scheduler(self._holding_scheduler("24h"))
        await handle_job_create(
            spec={"suspend": True},
            meta=self._meta(**{ANNOTATION_MAX_DELAY: "8h"}),
            name="short",
            namespace=NS,
            patch={},
        )
        assert _hold_limit("short") == (self.CREATED + timedelta(hours=8)).timestamp()

    async def test_no_series_without_max_delay(self) -> None:
        set_scheduler(self._holding_scheduler(""))
        await handle_job_create(
            spec={"suspend": True}, meta=self._meta(), name="uncapped", namespace=NS, patch={}
        )
        assert _hold_limit("uncapped") is None

    async def test_released_job_has_no_series(self) -> None:
        scheduler = JobScheduler(settings=Settings())
        scheduler.calculate_schedule = AsyncMock(  # type: ignore[method-assign]
            return_value=ScheduleDecision.release_now()
        )
        set_scheduler(scheduler)
        await handle_job_create(
            spec={"suspend": True}, meta=self._meta(), name="now", namespace=NS, patch={}
        )
        assert _hold_limit("now") is None

    async def test_timer_rebuilds_after_restart_and_clears_on_release(self) -> None:
        scheduler = JobScheduler(settings=Settings(max_delay="24h"))
        set_scheduler(scheduler)
        planned = datetime.now(UTC) + timedelta(hours=2)
        meta = self._meta(**{ANNOTATION_PLANNED_START: planned.isoformat()})

        # A fresh controller learns the limit from the Job's annotations on the first tick.
        await handle_job_timer(spec={"suspend": True}, meta=meta, name="t", namespace=NS, patch={})
        assert _hold_limit("t") == (self.CREATED + timedelta(hours=24)).timestamp()

        scheduler.should_release_now = MagicMock(return_value=True)  # type: ignore[method-assign]
        await handle_job_timer(spec={"suspend": True}, meta=meta, name="t", namespace=NS, patch={})
        assert _hold_limit("t") is None

    async def test_delete_forgets_held_job(self) -> None:
        set_scheduler(self._holding_scheduler("24h"))
        await handle_job_create(
            spec={"suspend": True}, meta=self._meta(), name="gone", namespace=NS, patch={}
        )
        track_held_job(NS, "gone")
        assert get_jobs_held_count(NS) == 1.0

        await handle_job_delete(name="gone", namespace=NS)
        assert get_jobs_held_count(NS) == 0.0
        assert _hold_limit("gone") is None
