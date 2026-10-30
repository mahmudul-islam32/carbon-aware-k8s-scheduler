"""Unit tests for the carbon-aware scheduler controller.

Tests cover:
- Annotation parsing and validation (F-01)
- Scheduler logic with mocked grid client and planner
- Fail-open behavior when grid data is unavailable (F-05)
- Event handler behavior
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from carbon_scheduler.config import (
    ANNOTATION_DEADLINE,
    ANNOTATION_ESTIMATED_RUNTIME,
    ANNOTATION_FALLBACK_REASON,
    ANNOTATION_FLEXIBLE,
    ANNOTATION_PLANNED_START,
    ANNOTATION_POLICY_USED,
)
from carbon_scheduler.controller.annotations import (
    AnnotationError,
    ParsedAnnotations,
    format_annotation_value,
    is_flexible_job,
    parse_annotations,
    parse_duration,
    parse_iso8601_datetime,
    validate_deadline,
)
from carbon_scheduler.controller.handlers import (
    handle_job_create,
    handle_job_timer,
    set_scheduler,
    should_process_job,
)
from carbon_scheduler.controller.scheduler import JobScheduler, ScheduleDecision
from carbon_scheduler.grid import GridData
from carbon_scheduler.grid.models import RenewableShareForecast


class TestParseDuration:
    """Tests for duration string parsing."""

    def test_parse_hours(self) -> None:
        """Should parse hour duration."""
        result = parse_duration("2h")
        assert result == timedelta(hours=2)

    def test_parse_minutes(self) -> None:
        """Should parse minute duration."""
        result = parse_duration("30m")
        assert result == timedelta(minutes=30)

    def test_parse_seconds(self) -> None:
        """Should parse second duration."""
        result = parse_duration("3600s")
        assert result == timedelta(seconds=3600)

    def test_parse_days(self) -> None:
        """Should parse day duration."""
        result = parse_duration("1d")
        assert result == timedelta(days=1)

    def test_parse_combined(self) -> None:
        """Should parse combined duration like '1h30m'."""
        result = parse_duration("1h30m")
        assert result == timedelta(hours=1, minutes=30)

    def test_parse_complex(self) -> None:
        """Should parse complex duration like '1d2h30m45s'."""
        result = parse_duration("1d2h30m45s")
        expected = timedelta(days=1, hours=2, minutes=30, seconds=45)
        assert result == expected

    def test_parse_numeric_only(self) -> None:
        """Should parse numeric string as seconds."""
        result = parse_duration("3600")
        assert result == timedelta(seconds=3600)

    def test_parse_with_whitespace(self) -> None:
        """Should handle whitespace."""
        result = parse_duration("  2h  ")
        assert result == timedelta(hours=2)

    def test_parse_empty_raises(self) -> None:
        """Should raise for empty string."""
        with pytest.raises(AnnotationError) as exc_info:
            parse_duration("")
        assert "empty" in str(exc_info.value).lower()

    def test_parse_invalid_raises(self) -> None:
        """Should raise for invalid format."""
        with pytest.raises(AnnotationError) as exc_info:
            parse_duration("invalid")
        assert "cannot parse" in str(exc_info.value).lower()


class TestParseIso8601Datetime:
    """Tests for ISO 8601 datetime parsing."""

    def test_parse_with_z_suffix(self) -> None:
        """Should parse datetime with Z suffix."""
        result = parse_iso8601_datetime("2024-06-15T10:00:00Z")
        expected = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        assert result == expected

    def test_parse_with_offset(self) -> None:
        """Should parse datetime with timezone offset."""
        result = parse_iso8601_datetime("2024-06-15T10:00:00+02:00")
        assert result.tzinfo is not None
        assert result.hour == 10

    def test_parse_without_timezone(self) -> None:
        """Should add UTC timezone if not specified."""
        result = parse_iso8601_datetime("2024-06-15T10:00:00")
        assert result.tzinfo == UTC

    def test_parse_empty_raises(self) -> None:
        """Should raise for empty string."""
        with pytest.raises(AnnotationError) as exc_info:
            parse_iso8601_datetime("")
        assert "empty" in str(exc_info.value).lower()

    def test_parse_invalid_raises(self) -> None:
        """Should raise for invalid format."""
        with pytest.raises(AnnotationError) as exc_info:
            parse_iso8601_datetime("not-a-date")
        assert "cannot parse" in str(exc_info.value).lower()


class TestIsFlexibleJob:
    """Tests for flexible job detection."""

    def test_flexible_true(self) -> None:
        """Should return True when flexible annotation is 'true'."""
        annotations = {ANNOTATION_FLEXIBLE: "true"}
        assert is_flexible_job(annotations) is True

    def test_flexible_true_uppercase(self) -> None:
        """Should handle uppercase 'TRUE'."""
        annotations = {ANNOTATION_FLEXIBLE: "TRUE"}
        assert is_flexible_job(annotations) is True

    def test_flexible_false(self) -> None:
        """Should return False when flexible annotation is 'false'."""
        annotations = {ANNOTATION_FLEXIBLE: "false"}
        assert is_flexible_job(annotations) is False

    def test_flexible_missing(self) -> None:
        """Should return False when annotation is missing."""
        annotations: dict[str, str] = {}
        assert is_flexible_job(annotations) is False

    def test_flexible_none(self) -> None:
        """Should return False when annotations is None."""
        assert is_flexible_job(None) is False

    def test_flexible_other_value(self) -> None:
        """Should return False for any value other than 'true'."""
        annotations = {ANNOTATION_FLEXIBLE: "yes"}
        assert is_flexible_job(annotations) is False


class TestParseAnnotations:
    """Tests for full annotation parsing."""

    def test_parse_valid_annotations(self) -> None:
        """Should parse valid annotations."""
        annotations = {
            ANNOTATION_FLEXIBLE: "true",
            ANNOTATION_DEADLINE: "2024-06-15T14:00:00Z",
            ANNOTATION_ESTIMATED_RUNTIME: "2h",
        }

        result = parse_annotations(annotations)

        assert result.flexible is True
        assert result.deadline == datetime(2024, 6, 15, 14, 0, 0, tzinfo=UTC)
        assert result.estimated_runtime == timedelta(hours=2)

    def test_parse_with_planned_start(self) -> None:
        """Should parse optional planned-start annotation."""
        annotations = {
            ANNOTATION_FLEXIBLE: "true",
            ANNOTATION_DEADLINE: "2024-06-15T14:00:00Z",
            ANNOTATION_ESTIMATED_RUNTIME: "2h",
            ANNOTATION_PLANNED_START: "2024-06-15T12:00:00Z",
        }

        result = parse_annotations(annotations)

        assert result.planned_start == datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)

    def test_parse_with_policy_used(self) -> None:
        """Should parse optional policy-used annotation."""
        annotations = {
            ANNOTATION_FLEXIBLE: "true",
            ANNOTATION_DEADLINE: "2024-06-15T14:00:00Z",
            ANNOTATION_ESTIMATED_RUNTIME: "2h",
            ANNOTATION_POLICY_USED: "greenest-window",
        }

        result = parse_annotations(annotations)

        assert result.policy_used == "greenest-window"

    def test_parse_missing_flexible_raises(self) -> None:
        """Should raise when flexible annotation is missing."""
        annotations = {
            ANNOTATION_DEADLINE: "2024-06-15T14:00:00Z",
            ANNOTATION_ESTIMATED_RUNTIME: "2h",
        }

        with pytest.raises(AnnotationError) as exc_info:
            parse_annotations(annotations)
        assert ANNOTATION_FLEXIBLE in str(exc_info.value)

    def test_parse_missing_deadline_raises(self) -> None:
        """Should raise when deadline annotation is missing."""
        annotations = {
            ANNOTATION_FLEXIBLE: "true",
            ANNOTATION_ESTIMATED_RUNTIME: "2h",
        }

        with pytest.raises(AnnotationError) as exc_info:
            parse_annotations(annotations)
        assert "deadline" in str(exc_info.value).lower()

    def test_parse_missing_runtime_raises(self) -> None:
        """Should raise when estimated-runtime annotation is missing."""
        annotations = {
            ANNOTATION_FLEXIBLE: "true",
            ANNOTATION_DEADLINE: "2024-06-15T14:00:00Z",
        }

        with pytest.raises(AnnotationError) as exc_info:
            parse_annotations(annotations)
        assert "runtime" in str(exc_info.value).lower()

    def test_parse_invalid_deadline_raises(self) -> None:
        """Should raise when deadline is invalid."""
        annotations = {
            ANNOTATION_FLEXIBLE: "true",
            ANNOTATION_DEADLINE: "not-a-date",
            ANNOTATION_ESTIMATED_RUNTIME: "2h",
        }

        with pytest.raises(AnnotationError) as exc_info:
            parse_annotations(annotations)
        assert "datetime" in str(exc_info.value).lower() or "deadline" in str(exc_info.value).lower()

    def test_parse_invalid_runtime_raises(self) -> None:
        """Should raise when runtime is invalid."""
        annotations = {
            ANNOTATION_FLEXIBLE: "true",
            ANNOTATION_DEADLINE: "2024-06-15T14:00:00Z",
            ANNOTATION_ESTIMATED_RUNTIME: "invalid",
        }

        with pytest.raises(AnnotationError) as exc_info:
            parse_annotations(annotations)
        assert "duration" in str(exc_info.value).lower() or "parse" in str(exc_info.value).lower()

    def test_parse_none_raises(self) -> None:
        """Should raise when annotations is None."""
        with pytest.raises(AnnotationError):
            parse_annotations(None)


class TestValidateDeadline:
    """Tests for deadline validation."""

    def test_valid_deadline(self) -> None:
        """Should validate achievable deadline."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        deadline = now + timedelta(hours=4)
        runtime = timedelta(hours=2)
        safety_margin = timedelta(minutes=5)

        is_valid, error = validate_deadline(deadline, runtime, safety_margin, now)

        assert is_valid is True
        assert error is None

    def test_deadline_too_soon(self) -> None:
        """Should reject deadline that cannot be met."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        deadline = now + timedelta(minutes=30)
        runtime = timedelta(hours=1)
        safety_margin = timedelta(minutes=5)

        is_valid, error = validate_deadline(deadline, runtime, safety_margin, now)

        assert is_valid is False
        assert error is not None
        assert "not achievable" in error.lower()

    def test_deadline_in_past(self) -> None:
        """Should reject deadline in the past."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        deadline = now - timedelta(hours=1)
        runtime = timedelta(hours=1)
        safety_margin = timedelta(minutes=5)

        is_valid, error = validate_deadline(deadline, runtime, safety_margin, now)

        assert is_valid is False
        assert error is not None
        assert "not achievable" in error.lower() or "passed" in error.lower()


class TestFormatAnnotationValue:
    """Tests for annotation value formatting."""

    def test_format_datetime(self) -> None:
        """Should format datetime as ISO 8601."""
        dt = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        result = format_annotation_value(dt)
        assert "2024-06-15" in result
        assert "10:00:00" in result

    def test_format_timedelta_hours(self) -> None:
        """Should format timedelta with hours."""
        td = timedelta(hours=2)
        result = format_annotation_value(td)
        assert result == "2h"

    def test_format_timedelta_minutes(self) -> None:
        """Should format timedelta with minutes."""
        td = timedelta(minutes=30)
        result = format_annotation_value(td)
        assert result == "30m"

    def test_format_timedelta_combined(self) -> None:
        """Should format combined timedelta."""
        td = timedelta(hours=1, minutes=30, seconds=45)
        result = format_annotation_value(td)
        assert "1h" in result
        assert "30m" in result
        assert "45s" in result

    def test_format_string(self) -> None:
        """Should pass through strings."""
        result = format_annotation_value("test")
        assert result == "test"


class TestScheduleDecision:
    """Tests for ScheduleDecision class."""

    def test_release_now(self) -> None:
        """Should create immediate release decision."""
        decision = ScheduleDecision.release_now(
            policy_used="immediate",
            is_fallback=False,
        )

        assert decision.should_release is True
        assert decision.policy_used == "immediate"
        assert decision.is_fallback is False
        assert ANNOTATION_PLANNED_START in decision.annotations_to_set

    def test_release_now_fallback(self) -> None:
        """Should create fallback release decision."""
        decision = ScheduleDecision.release_now(
            policy_used="greenest-window",
            is_fallback=True,
            fallback_reason="grid_data_unavailable",
        )

        assert decision.should_release is True
        assert decision.is_fallback is True
        assert decision.fallback_reason == "grid_data_unavailable"
        assert ANNOTATION_FALLBACK_REASON in decision.annotations_to_set

    def test_hold_until(self) -> None:
        """Should create hold decision."""
        planned_start = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
        decision = ScheduleDecision.hold_until(
            planned_start=planned_start,
            policy_used="greenest-window",
        )

        assert decision.should_release is False
        assert decision.planned_start == planned_start
        assert decision.policy_used == "greenest-window"


class TestJobScheduler:
    """Tests for JobScheduler class."""

    @pytest.fixture
    def mock_grid_client(self) -> MagicMock:
        """Create a mock grid client."""
        client = MagicMock()
        client.get_grid_data = AsyncMock()
        return client

    @pytest.fixture
    def scheduler(self, mock_grid_client: MagicMock) -> JobScheduler:
        """Create a scheduler with mock grid client."""
        scheduler = JobScheduler()
        scheduler.grid_client = mock_grid_client
        return scheduler

    @pytest.mark.asyncio
    async def test_calculate_schedule_valid_job(
        self,
        scheduler: JobScheduler,
        mock_grid_client: MagicMock,
    ) -> None:
        """Should calculate schedule for valid job."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        deadline = now + timedelta(hours=4)

        forecast = RenewableShareForecast(
            unix_seconds=[
                int((now + timedelta(minutes=i * 15)).timestamp())
                for i in range(16)
            ],
            ren_share=[50, 55, 60, 70, 80, 85, 90, 85, 80, 75, 70, 65, 60, 55, 50, 45],
            substitute=False,
        )

        grid_data = GridData(
            forecast=forecast,
            fetched_at=now,
            country="de",
        )
        mock_grid_client.get_grid_data.return_value = grid_data

        annotations = {
            ANNOTATION_FLEXIBLE: "true",
            ANNOTATION_DEADLINE: deadline.isoformat(),
            ANNOTATION_ESTIMATED_RUNTIME: "30m",
        }

        decision = await scheduler.calculate_schedule(
            job_name="test-job",
            job_namespace="default",
            annotations=annotations,
            current_time=now,
        )

        assert decision.policy_used is not None
        assert decision.planned_start is not None or decision.should_release

    @pytest.mark.asyncio
    async def test_calculate_schedule_invalid_annotations(
        self,
        scheduler: JobScheduler,
    ) -> None:
        """Should release immediately for invalid annotations (fail-open)."""
        annotations = {
            ANNOTATION_FLEXIBLE: "true",
            ANNOTATION_DEADLINE: "invalid-date",
            ANNOTATION_ESTIMATED_RUNTIME: "30m",
        }

        decision = await scheduler.calculate_schedule(
            job_name="test-job",
            job_namespace="default",
            annotations=annotations,
        )

        assert decision.should_release is True
        assert decision.is_fallback is True
        assert "validation_error" in (decision.fallback_reason or "")

    @pytest.mark.asyncio
    async def test_calculate_schedule_no_grid_client(self) -> None:
        """Should release immediately when grid client not configured."""
        scheduler = JobScheduler()

        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        deadline = now + timedelta(hours=4)

        annotations = {
            ANNOTATION_FLEXIBLE: "true",
            ANNOTATION_DEADLINE: deadline.isoformat(),
            ANNOTATION_ESTIMATED_RUNTIME: "30m",
        }

        decision = await scheduler.calculate_schedule(
            job_name="test-job",
            job_namespace="default",
            annotations=annotations,
            current_time=now,
        )

        assert decision.should_release is True
        assert decision.is_fallback is True
        assert "unavailable" in (decision.fallback_reason or "")

    @pytest.mark.asyncio
    async def test_calculate_schedule_grid_data_unavailable(
        self,
        scheduler: JobScheduler,
        mock_grid_client: MagicMock,
    ) -> None:
        """Should release immediately when grid data is unavailable (F-05)."""
        mock_grid_client.get_grid_data.return_value = None

        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        deadline = now + timedelta(hours=4)

        annotations = {
            ANNOTATION_FLEXIBLE: "true",
            ANNOTATION_DEADLINE: deadline.isoformat(),
            ANNOTATION_ESTIMATED_RUNTIME: "30m",
        }

        decision = await scheduler.calculate_schedule(
            job_name="test-job",
            job_namespace="default",
            annotations=annotations,
            current_time=now,
        )

        assert decision.should_release is True
        assert decision.is_fallback is True
        assert "unavailable" in (decision.fallback_reason or "")

    @pytest.mark.asyncio
    async def test_calculate_schedule_deadline_too_soon(
        self,
        scheduler: JobScheduler,
        mock_grid_client: MagicMock,
    ) -> None:
        """Should release immediately when deadline is too soon."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        deadline = now + timedelta(minutes=20)

        forecast = RenewableShareForecast(
            unix_seconds=[int(now.timestamp())],
            ren_share=[90],
            substitute=False,
        )

        grid_data = GridData(
            forecast=forecast,
            fetched_at=now,
            country="de",
        )
        mock_grid_client.get_grid_data.return_value = grid_data

        annotations = {
            ANNOTATION_FLEXIBLE: "true",
            ANNOTATION_DEADLINE: deadline.isoformat(),
            ANNOTATION_ESTIMATED_RUNTIME: "30m",
        }

        decision = await scheduler.calculate_schedule(
            job_name="test-job",
            job_namespace="default",
            annotations=annotations,
            current_time=now,
        )

        assert decision.should_release is True
        assert decision.is_fallback is True

    def test_should_release_now_planned_start_reached(
        self,
        scheduler: JobScheduler,
    ) -> None:
        """Should release when planned start time is reached."""
        now = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
        planned_start = datetime(2024, 6, 15, 11, 55, 0, tzinfo=UTC)

        parsed = ParsedAnnotations(
            flexible=True,
            deadline=now + timedelta(hours=2),
            estimated_runtime=timedelta(hours=1),
            planned_start=planned_start,
        )

        assert scheduler.should_release_now(parsed, now) is True

    def test_should_release_now_planned_start_not_reached(
        self,
        scheduler: JobScheduler,
    ) -> None:
        """Should not release when planned start time is not reached."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        planned_start = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)

        parsed = ParsedAnnotations(
            flexible=True,
            deadline=now + timedelta(hours=4),
            estimated_runtime=timedelta(hours=1),
            planned_start=planned_start,
        )

        assert scheduler.should_release_now(parsed, now) is False

    def test_should_release_now_deadline_approaching(
        self,
        scheduler: JobScheduler,
    ) -> None:
        """Should release when deadline minus safety margin is reached."""
        now = datetime(2024, 6, 15, 13, 30, 0, tzinfo=UTC)
        deadline = datetime(2024, 6, 15, 14, 0, 0, tzinfo=UTC)

        parsed = ParsedAnnotations(
            flexible=True,
            deadline=deadline,
            estimated_runtime=timedelta(minutes=30),
            planned_start=None,
        )

        assert scheduler.should_release_now(parsed, now) is True

    def test_calculate_delay_seconds(
        self,
        scheduler: JobScheduler,
    ) -> None:
        """Should calculate delay correctly."""
        submit_time = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        release_time = datetime(2024, 6, 15, 12, 30, 0, tzinfo=UTC)

        delay = scheduler.calculate_delay_seconds(submit_time, release_time)

        expected = 2.5 * 3600
        assert delay == expected


class TestShouldProcessJob:
    """Tests for should_process_job function."""

    def test_suspended_flexible_job(self) -> None:
        """Should process suspended job with flexible annotation."""
        spec = {"suspend": True}
        meta = {"annotations": {ANNOTATION_FLEXIBLE: "true"}}

        assert should_process_job(spec, meta) is True

    def test_not_suspended_job(self) -> None:
        """Should not process job without suspend=true."""
        spec = {"suspend": False}
        meta = {"annotations": {ANNOTATION_FLEXIBLE: "true"}}

        assert should_process_job(spec, meta) is False

    def test_suspended_not_flexible(self) -> None:
        """Should not process suspended job without flexible annotation."""
        spec = {"suspend": True}
        meta = {"annotations": {}}

        assert should_process_job(spec, meta) is False

    def test_no_annotations(self) -> None:
        """Should not process job without annotations."""
        spec = {"suspend": True}
        meta = {}

        assert should_process_job(spec, meta) is False


class TestHandleJobCreate:
    """Tests for handle_job_create handler."""

    @pytest.fixture
    def mock_scheduler(self) -> MagicMock:
        """Create a mock scheduler."""
        scheduler = MagicMock(spec=JobScheduler)
        scheduler.replan = AsyncMock(return_value=None)
        scheduler.calculate_schedule = AsyncMock()
        return scheduler

    @pytest.mark.asyncio
    async def test_ignores_non_flexible_job(self) -> None:
        """Should ignore jobs without flexible annotation."""
        spec = {"suspend": True}
        meta = {"annotations": {}}
        patch: dict[str, Any] = {}

        result = await handle_job_create(
            spec=spec,
            meta=meta,
            name="test-job",
            namespace="default",
            patch=patch,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_releases_on_validation_error(self) -> None:
        """Should release job with invalid annotations (fail-open)."""
        spec = {"suspend": True}
        meta = {
            "annotations": {
                ANNOTATION_FLEXIBLE: "true",
                ANNOTATION_DEADLINE: "invalid",
                ANNOTATION_ESTIMATED_RUNTIME: "30m",
            }
        }
        patch: dict[str, Any] = {}

        result = await handle_job_create(
            spec=spec,
            meta=meta,
            name="test-job",
            namespace="default",
            patch=patch,
        )

        assert result is not None
        assert result["released"] is True
        assert result["fallback"] is True
        assert patch["spec"]["suspend"] is False

    @pytest.mark.asyncio
    async def test_holds_job_for_green_window(
        self,
        mock_scheduler: MagicMock,
    ) -> None:
        """Should hold job when green window is in the future."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        planned_start = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)

        mock_scheduler.calculate_schedule.return_value = ScheduleDecision.hold_until(
            planned_start=planned_start,
            policy_used="greenest-window",
        )

        set_scheduler(mock_scheduler)

        spec = {"suspend": True}
        meta = {
            "annotations": {
                ANNOTATION_FLEXIBLE: "true",
                ANNOTATION_DEADLINE: (now + timedelta(hours=4)).isoformat(),
                ANNOTATION_ESTIMATED_RUNTIME: "30m",
            }
        }
        patch: dict[str, Any] = {}

        result = await handle_job_create(
            spec=spec,
            meta=meta,
            name="test-job",
            namespace="default",
            patch=patch,
        )

        assert result is not None
        assert result["released"] is False
        assert result["policy"] == "greenest-window"
        assert "suspend" not in patch.get("spec", {})

    @pytest.mark.asyncio
    async def test_releases_job_immediately(
        self,
        mock_scheduler: MagicMock,
    ) -> None:
        """Should release job when policy says so."""
        mock_scheduler.calculate_schedule.return_value = ScheduleDecision.release_now(
            policy_used="immediate",
            is_fallback=False,
        )

        set_scheduler(mock_scheduler)

        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        spec = {"suspend": True}
        meta = {
            "annotations": {
                ANNOTATION_FLEXIBLE: "true",
                ANNOTATION_DEADLINE: (now + timedelta(hours=4)).isoformat(),
                ANNOTATION_ESTIMATED_RUNTIME: "30m",
            }
        }
        patch: dict[str, Any] = {}

        result = await handle_job_create(
            spec=spec,
            meta=meta,
            name="test-job",
            namespace="default",
            patch=patch,
        )

        assert result is not None
        assert result["released"] is True
        assert patch["spec"]["suspend"] is False


class TestHandleJobTimer:
    """Tests for handle_job_timer handler."""

    @pytest.fixture
    def mock_scheduler(self) -> MagicMock:
        """Create a mock scheduler."""
        scheduler = MagicMock(spec=JobScheduler)
        scheduler.replan = AsyncMock(return_value=None)
        scheduler.should_release_now = MagicMock(return_value=False)
        return scheduler

    @pytest.mark.asyncio
    async def test_ignores_unsuspended_job(self) -> None:
        """Should ignore jobs that are not suspended."""
        spec = {"suspend": False}
        meta = {
            "annotations": {
                ANNOTATION_FLEXIBLE: "true",
            }
        }
        patch: dict[str, Any] = {}

        result = await handle_job_timer(
            spec=spec,
            meta=meta,
            name="test-job",
            namespace="default",
            patch=patch,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_ignores_non_flexible_job(self) -> None:
        """Should ignore jobs without flexible annotation."""
        spec = {"suspend": True}
        meta = {"annotations": {}}
        patch: dict[str, Any] = {}

        result = await handle_job_timer(
            spec=spec,
            meta=meta,
            name="test-job",
            namespace="default",
            patch=patch,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_releases_when_planned_start_reached(
        self,
        mock_scheduler: MagicMock,
    ) -> None:
        """Should release job when planned start is reached."""
        mock_scheduler.should_release_now.return_value = True

        set_scheduler(mock_scheduler)

        now = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
        planned_start = datetime(2024, 6, 15, 11, 55, 0, tzinfo=UTC)

        spec = {"suspend": True}
        meta = {
            "annotations": {
                ANNOTATION_FLEXIBLE: "true",
                ANNOTATION_DEADLINE: (now + timedelta(hours=2)).isoformat(),
                ANNOTATION_ESTIMATED_RUNTIME: "30m",
                ANNOTATION_PLANNED_START: planned_start.isoformat(),
            }
        }
        patch: dict[str, Any] = {}

        result = await handle_job_timer(
            spec=spec,
            meta=meta,
            name="test-job",
            namespace="default",
            patch=patch,
        )

        assert result is not None
        assert result["released"] is True
        assert patch["spec"]["suspend"] is False

    @pytest.mark.asyncio
    async def test_does_not_release_early(
        self,
        mock_scheduler: MagicMock,
    ) -> None:
        """Should not release job before planned start."""
        mock_scheduler.should_release_now.return_value = False

        set_scheduler(mock_scheduler)

        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        planned_start = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)

        spec = {"suspend": True}
        meta = {
            "annotations": {
                ANNOTATION_FLEXIBLE: "true",
                ANNOTATION_DEADLINE: (now + timedelta(hours=4)).isoformat(),
                ANNOTATION_ESTIMATED_RUNTIME: "30m",
                ANNOTATION_PLANNED_START: planned_start.isoformat(),
            }
        }
        patch: dict[str, Any] = {}

        result = await handle_job_timer(
            spec=spec,
            meta=meta,
            name="test-job",
            namespace="default",
            patch=patch,
        )

        assert result is None
        assert "suspend" not in patch.get("spec", {})


class TestFailOpenBehavior:
    """Tests for fail-open behavior (F-05)."""

    @pytest.fixture
    def mock_grid_client(self) -> MagicMock:
        """Create a mock grid client."""
        client = MagicMock()
        client.get_grid_data = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_releases_when_grid_api_fails(
        self,
        mock_grid_client: MagicMock,
    ) -> None:
        """Should release immediately when grid API call fails."""
        from carbon_scheduler.grid import EnergyChartsError

        mock_grid_client.get_grid_data.side_effect = EnergyChartsError("API error")

        scheduler = JobScheduler()
        scheduler.grid_client = mock_grid_client

        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        deadline = now + timedelta(hours=4)

        annotations = {
            ANNOTATION_FLEXIBLE: "true",
            ANNOTATION_DEADLINE: deadline.isoformat(),
            ANNOTATION_ESTIMATED_RUNTIME: "30m",
        }

        decision = await scheduler.calculate_schedule(
            job_name="test-job",
            job_namespace="default",
            annotations=annotations,
            current_time=now,
        )

        assert decision.should_release is True
        assert decision.is_fallback is True

    @pytest.mark.asyncio
    async def test_releases_when_forecast_degraded(
        self,
        mock_grid_client: MagicMock,
    ) -> None:
        """Should release immediately when forecast data is degraded."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)

        forecast = RenewableShareForecast(
            unix_seconds=[int(now.timestamp())],
            ren_share=[90],
            substitute=True,
        )

        grid_data = GridData(
            forecast=forecast,
            fetched_at=now,
            country="de",
        )
        mock_grid_client.get_grid_data.return_value = grid_data

        scheduler = JobScheduler()
        scheduler.grid_client = mock_grid_client

        deadline = now + timedelta(hours=4)

        annotations = {
            ANNOTATION_FLEXIBLE: "true",
            ANNOTATION_DEADLINE: deadline.isoformat(),
            ANNOTATION_ESTIMATED_RUNTIME: "30m",
        }

        decision = await scheduler.calculate_schedule(
            job_name="test-job",
            job_namespace="default",
            annotations=annotations,
            current_time=now,
        )

        assert decision.should_release is True
        assert decision.is_fallback is True
        assert "degraded" in (decision.fallback_reason or "")

    @pytest.mark.asyncio
    async def test_releases_at_latest_feasible_start(
        self,
        mock_grid_client: MagicMock,
    ) -> None:
        """Should release at latest feasible start when deadline approaches."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)

        forecast = RenewableShareForecast(
            unix_seconds=[
                int((now + timedelta(minutes=i * 15)).timestamp())
                for i in range(16)
            ],
            ren_share=[30] * 16,
            substitute=False,
        )

        grid_data = GridData(
            forecast=forecast,
            fetched_at=now,
            country="de",
        )
        mock_grid_client.get_grid_data.return_value = grid_data

        scheduler = JobScheduler()
        scheduler.grid_client = mock_grid_client
        scheduler.settings.policy = "threshold"
        scheduler.settings.renewable_threshold_pct = 80

        deadline = now + timedelta(hours=4)

        annotations = {
            ANNOTATION_FLEXIBLE: "true",
            ANNOTATION_DEADLINE: deadline.isoformat(),
            ANNOTATION_ESTIMATED_RUNTIME: "30m",
        }

        decision = await scheduler.calculate_schedule(
            job_name="test-job",
            job_namespace="default",
            annotations=annotations,
            current_time=now,
        )

        assert decision.is_fallback is True


class TestStateRecovery:
    """Tests for state recovery after controller restart (F-02)."""

    @pytest.fixture
    def mock_scheduler(self) -> MagicMock:
        """Create a mock scheduler."""
        scheduler = MagicMock(spec=JobScheduler)
        scheduler.replan = AsyncMock(return_value=None)
        scheduler.should_release_now = MagicMock()
        return scheduler

    @pytest.mark.asyncio
    async def test_recovers_state_from_annotations(
        self,
        mock_scheduler: MagicMock,
    ) -> None:
        """Should rebuild state from existing annotations on restart."""
        mock_scheduler.should_release_now.return_value = False

        set_scheduler(mock_scheduler)

        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        planned_start = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)

        spec = {"suspend": True}
        meta = {
            "annotations": {
                ANNOTATION_FLEXIBLE: "true",
                ANNOTATION_DEADLINE: (now + timedelta(hours=4)).isoformat(),
                ANNOTATION_ESTIMATED_RUNTIME: "30m",
                ANNOTATION_PLANNED_START: planned_start.isoformat(),
                ANNOTATION_POLICY_USED: "greenest-window",
            }
        }
        patch: dict[str, Any] = {}

        await handle_job_timer(
            spec=spec,
            meta=meta,
            name="test-job",
            namespace="default",
            patch=patch,
        )

        mock_scheduler.should_release_now.assert_called_once()

        call_args = mock_scheduler.should_release_now.call_args
        parsed = call_args[0][0]
        assert parsed.planned_start == planned_start
        assert parsed.policy_used == "greenest-window"
