"""Tests for F-22: relative deadlines (``deadline-in``) for Jobs created by CronJobs."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from carbon_scheduler.config import (
    ANNOTATION_DEADLINE,
    ANNOTATION_DEADLINE_IN,
    ANNOTATION_DEADLINE_SOURCE,
    ANNOTATION_ESTIMATED_RUNTIME,
    ANNOTATION_FLEXIBLE,
    ANNOTATION_LATEST_START,
    ANNOTATION_PLANNED_START,
    DEADLINE_SOURCE_RELATIVE,
    Settings,
)
from carbon_scheduler.controller.annotations import AnnotationError, parse_annotations
from carbon_scheduler.controller.handlers import (
    handle_job_create,
    handle_job_timer,
    set_scheduler,
)
from carbon_scheduler.controller.scheduler import JobScheduler
from carbon_scheduler.grid import GridData
from carbon_scheduler.grid.models import RenewableShareForecast
from carbon_scheduler.metrics.prometheus import get_jobs_held_count, reset_jobs_held

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "batch-processing-cronjob.yaml"
CREATED = datetime(2026, 11, 1, 23, 0, tzinfo=UTC)
SHARES = [30, 30, 35, 40, 50, 60, 85, 90, 85, 50, 40, 35, 30, 30, 30, 30]


def _relative(deadline_in: str = "6h", **extra: str) -> dict[str, str]:
    annotations = {
        ANNOTATION_FLEXIBLE: "true",
        ANNOTATION_DEADLINE_IN: deadline_in,
        ANNOTATION_ESTIMATED_RUNTIME: "1h",
    }
    annotations.update(extra)
    return annotations


def _scheduler(start: datetime) -> JobScheduler:
    forecast = RenewableShareForecast(
        unix_seconds=[int((start + timedelta(hours=i)).timestamp()) for i in range(len(SHARES))],
        ren_share=SHARES,
        substitute=False,
    )
    client = MagicMock()
    client.get_grid_data = AsyncMock(
        return_value=GridData(forecast=forecast, fetched_at=start, country="de")
    )
    scheduler = JobScheduler(settings=Settings(max_delay=""))
    scheduler.grid_client = client
    return scheduler


def _cronjob_template_annotations() -> dict[str, str]:
    cronjob = yaml.safe_load(EXAMPLE.read_text())
    return dict(cronjob["spec"]["jobTemplate"]["metadata"]["annotations"])


class TestParseDeadlineIn:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("6h", timedelta(hours=6)),
            ("1h30m", timedelta(hours=1, minutes=30)),
            ("90m", timedelta(minutes=90)),
            ("3600s", timedelta(hours=1)),
            ("1d", timedelta(days=1)),
            ("7200", timedelta(hours=2)),
        ],
    )
    def test_accepts_same_formats_as_estimated_runtime(
        self, value: str, expected: timedelta
    ) -> None:
        parsed = parse_annotations(_relative(value), created_at=CREATED)
        assert parsed.deadline_in == expected
        assert parsed.deadline == CREATED + expected
        assert parsed.deadline_resolved_now is True

    @pytest.mark.parametrize("value", ["soon", "", "0h"])
    def test_invalid_value_raises(self, value: str) -> None:
        with pytest.raises(AnnotationError) as exc:
            parse_annotations(_relative(value), created_at=CREATED)
        assert exc.value.annotation_key == ANNOTATION_DEADLINE_IN

    def test_naive_creation_time_is_utc(self) -> None:
        parsed = parse_annotations(_relative("2h"), created_at=CREATED.replace(tzinfo=None))
        assert parsed.deadline == CREATED + timedelta(hours=2)

    def test_defaults_to_now_without_creation_time(self) -> None:
        before = datetime.now(UTC)
        parsed = parse_annotations(_relative("2h"))
        assert before + timedelta(hours=2) <= parsed.deadline <= datetime.now(UTC) + timedelta(
            hours=2
        )

    def test_absolute_deadline_still_works(self) -> None:
        annotations = {
            ANNOTATION_FLEXIBLE: "true",
            ANNOTATION_DEADLINE: "2026-11-02T06:00:00+00:00",
            ANNOTATION_ESTIMATED_RUNTIME: "1h",
        }
        parsed = parse_annotations(annotations, created_at=CREATED)
        assert parsed.deadline == datetime(2026, 11, 2, 6, 0, tzinfo=UTC)
        assert parsed.deadline_in is None
        assert parsed.deadline_resolved_now is False


class TestExactlyOneDeadline:
    def test_both_raise_validation_error(self) -> None:
        annotations = _relative("6h", **{ANNOTATION_DEADLINE: "2026-11-02T06:00:00+00:00"})
        with pytest.raises(AnnotationError, match="only one"):
            parse_annotations(annotations, created_at=CREATED)

    def test_neither_raises(self) -> None:
        annotations = {ANNOTATION_FLEXIBLE: "true", ANNOTATION_ESTIMATED_RUNTIME: "1h"}
        with pytest.raises(AnnotationError) as exc:
            parse_annotations(annotations, created_at=CREATED)
        assert ANNOTATION_DEADLINE in str(exc.value)
        assert ANNOTATION_DEADLINE_IN in str(exc.value)

    def test_resolved_value_written_by_controller_is_accepted(self) -> None:
        resolved = CREATED + timedelta(hours=6)
        annotations = _relative(
            "6h",
            **{
                ANNOTATION_DEADLINE: resolved.isoformat(),
                ANNOTATION_DEADLINE_SOURCE: DEADLINE_SOURCE_RELATIVE,
            },
        )
        # A later reconcile must reuse the stored value, not re-resolve from "now".
        parsed = parse_annotations(annotations, created_at=CREATED + timedelta(hours=3))
        assert parsed.deadline == resolved
        assert parsed.deadline_resolved_now is False

    async def test_both_on_create_is_validation_error_and_fail_open(self) -> None:
        set_scheduler(_scheduler(CREATED))
        patch: dict[str, Any] = {}
        result = await handle_job_create(
            spec={"suspend": True},
            meta={
                "annotations": _relative(
                    "6h", **{ANNOTATION_DEADLINE: "2026-11-02T06:00:00+00:00"}
                ),
                "creationTimestamp": CREATED.isoformat(),
            },
            name="both",
            namespace="f22-ns",
            patch=patch,
        )
        assert result is not None and result["fallback"] is True
        assert patch["spec"]["suspend"] is False


class TestScheduler:
    async def test_resolves_from_creation_time_and_writes_back(self) -> None:
        now = CREATED + timedelta(minutes=2)
        decision = await _scheduler(now).calculate_schedule(
            "job", "ns", _relative("12h"), current_time=now, created_at=CREATED
        )
        assert decision.should_release is False
        assert decision.annotations_to_set[ANNOTATION_DEADLINE] == (
            CREATED + timedelta(hours=12)
        ).isoformat()
        assert decision.annotations_to_set[ANNOTATION_DEADLINE_SOURCE] == DEADLINE_SOURCE_RELATIVE
        # latest start = deadline - runtime - 5 min safety margin
        assert decision.annotations_to_set[ANNOTATION_LATEST_START] == (
            CREATED + timedelta(hours=10, minutes=55)
        ).isoformat()

    async def test_writes_back_on_immediate_release_too(self) -> None:
        scheduler = JobScheduler(settings=Settings(max_delay=""))  # no grid client → fail-open
        decision = await scheduler.calculate_schedule(
            "job", "ns", _relative("12h"), current_time=CREATED, created_at=CREATED
        )
        assert decision.should_release is True
        assert decision.annotations_to_set[ANNOTATION_DEADLINE_SOURCE] == DEADLINE_SOURCE_RELATIVE

    async def test_absolute_deadline_is_not_rewritten(self) -> None:
        annotations = {
            ANNOTATION_FLEXIBLE: "true",
            ANNOTATION_DEADLINE: (CREATED + timedelta(hours=12)).isoformat(),
            ANNOTATION_ESTIMATED_RUNTIME: "1h",
        }
        decision = await _scheduler(CREATED).calculate_schedule(
            "job", "ns", annotations, current_time=CREATED
        )
        assert ANNOTATION_DEADLINE not in decision.annotations_to_set
        assert ANNOTATION_DEADLINE_SOURCE not in decision.annotations_to_set


class TestCronJobCreatedJob:
    @pytest.fixture(autouse=True)
    def _reset(self) -> None:
        reset_jobs_held("data-pipelines")

    def test_example_uses_deadline_in(self) -> None:
        annotations = _cronjob_template_annotations()
        assert annotations[ANNOTATION_DEADLINE_IN] == "12h"
        assert ANNOTATION_DEADLINE not in annotations

    @staticmethod
    def _job_meta(created: datetime) -> dict[str, Any]:
        """Metadata of a Job the CronJob controller created from the example template."""
        return {
            "name": f"weekly-data-sync-{int(created.timestamp()) // 60}",
            "namespace": "data-pipelines",
            "creationTimestamp": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ownerReferences": [{"apiVersion": "batch/v1", "kind": "CronJob", "name": "weekly-data-sync"}],
            "annotations": _cronjob_template_annotations(),
        }

    async def test_each_run_gets_its_own_deadline(self) -> None:
        deadlines = []
        for created in (CREATED, CREATED + timedelta(days=7)):
            set_scheduler(_scheduler(created))
            meta = self._job_meta(created)
            patch: dict[str, Any] = {}
            result = await handle_job_create(
                spec={"suspend": True},
                meta=meta,
                name=meta["name"],
                namespace="data-pipelines",
                patch=patch,
            )
            assert result is not None and result["released"] is False
            written = patch["metadata"]["annotations"]
            assert written[ANNOTATION_DEADLINE] == (created + timedelta(hours=12)).isoformat()
            assert written[ANNOTATION_DEADLINE_SOURCE] == DEADLINE_SOURCE_RELATIVE
            deadlines.append(written[ANNOTATION_DEADLINE])
        assert deadlines[0] != deadlines[1]

    async def test_timer_keeps_resolved_deadline_after_write_back(self) -> None:
        set_scheduler(_scheduler(CREATED))
        meta = self._job_meta(CREATED)
        patch: dict[str, Any] = {}
        await handle_job_create(
            spec={"suspend": True},
            meta=meta,
            name=meta["name"],
            namespace="data-pipelines",
            patch=patch,
        )
        meta["annotations"].update(patch["metadata"]["annotations"])

        parsed = parse_annotations(meta["annotations"], created_at=CREATED)
        assert parsed.deadline == CREATED + timedelta(hours=12)
        assert parsed.planned_start is not None
        assert meta["annotations"][ANNOTATION_PLANNED_START] == parsed.planned_start.isoformat()

        # The timer re-parses the merged annotations (deadline + deadline-in + marker)
        # without a ValidationError, so the Job stays held and is tracked. CREATED is
        # in the future relative to the wall clock, so nothing is due yet.
        timer_patch: dict[str, Any] = {}
        result = await handle_job_timer(
            spec={"suspend": True},
            meta=meta,
            name=meta["name"],
            namespace="data-pipelines",
            patch=timer_patch,
        )
        assert result is None
        assert timer_patch == {}
        assert get_jobs_held_count("data-pipelines") == 1.0
