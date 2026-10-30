"""Annotation parsing and validation for carbon-aware scheduling.

This module handles parsing and validation of job annotations according to F-01:
- Jobs must have the flexible annotation set to "true"
- Required annotations: deadline (ISO 8601), estimated-runtime (duration string)
- Invalid values cause Warning Events and immediate release (fail-open)
"""

import contextlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from carbon_scheduler.config import (
    ANNOTATION_DEADLINE,
    ANNOTATION_ESTIMATED_RUNTIME,
    ANNOTATION_FALLBACK_REASON,
    ANNOTATION_FLEXIBLE,
    ANNOTATION_LATEST_START,
    ANNOTATION_MAX_DELAY,
    ANNOTATION_PLANNED_AT,
    ANNOTATION_PLANNED_START,
    ANNOTATION_POLICY,
    ANNOTATION_POLICY_USED,
    ANNOTATION_RENEWABLE_THRESHOLD,
)

VALID_POLICIES = ("immediate", "threshold", "greenest-window")


class AnnotationError(Exception):
    """Raised when job annotations are invalid."""

    def __init__(self, message: str, annotation_key: str | None = None):
        self.annotation_key = annotation_key
        super().__init__(message)


@dataclass
class ParsedAnnotations:
    """Parsed and validated job annotations."""

    flexible: bool
    deadline: datetime
    estimated_runtime: timedelta
    planned_start: datetime | None = None
    policy_used: str | None = None
    fallback_reason: str | None = None
    policy_override: str | None = None
    renewable_threshold_override: int | None = None
    max_delay: timedelta | None = None
    latest_start: datetime | None = None
    planned_at: datetime | None = None

    @property
    def estimated_runtime_seconds(self) -> float:
        """Get estimated runtime in seconds."""
        return self.estimated_runtime.total_seconds()


def parse_duration(
    duration_str: str,
    annotation_key: str = ANNOTATION_ESTIMATED_RUNTIME,
) -> timedelta:
    """Parse a duration string like '2h', '30m', '1h30m', '90s'.

    Supported units: d (days), h (hours), m (minutes), s (seconds)

    Args:
        duration_str: Duration string to parse
        annotation_key: Annotation reported in errors

    Returns:
        Parsed timedelta

    Raises:
        AnnotationError: If the duration string is invalid
    """
    if not duration_str:
        raise AnnotationError(
            "Duration string is empty",
            annotation_key=annotation_key,
        )

    s = duration_str.strip().lower()
    total_seconds = 0.0

    pattern = r"(\d+(?:\.\d+)?)(d|h|m|s)"
    matches = re.findall(pattern, s)

    if not matches:
        if s.replace(".", "").isdigit():
            return timedelta(seconds=float(s))
        raise AnnotationError(
            f"Cannot parse duration '{duration_str}'. "
            "Expected format like '2h', '30m', '1h30m', or '3600s'",
            annotation_key=annotation_key,
        )

    multipliers = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    for value, unit in matches:
        total_seconds += float(value) * multipliers[unit]

    if total_seconds <= 0:
        raise AnnotationError(
            f"Duration '{duration_str}' must be positive",
            annotation_key=annotation_key,
        )

    return timedelta(seconds=total_seconds)


def parse_iso8601_datetime(dt_str: str) -> datetime:
    """Parse an ISO 8601 datetime string.

    Args:
        dt_str: ISO 8601 datetime string

    Returns:
        Parsed datetime (timezone-aware)

    Raises:
        AnnotationError: If the datetime string is invalid
    """
    if not dt_str:
        raise AnnotationError(
            "Datetime string is empty",
            annotation_key=ANNOTATION_DEADLINE,
        )

    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError as e:
        raise AnnotationError(
            f"Cannot parse datetime '{dt_str}'. Expected ISO 8601 format "
            f"(e.g., '2024-01-15T10:00:00Z'): {e}",
            annotation_key=ANNOTATION_DEADLINE,
        ) from e


def is_flexible_job(annotations: dict[str, Any] | None) -> bool:
    """Check if a job has the flexible annotation set to 'true'.

    Args:
        annotations: Job metadata annotations

    Returns:
        True if the job should be managed by the carbon scheduler
    """
    if annotations is None:
        return False

    flexible_value = annotations.get(ANNOTATION_FLEXIBLE)
    if flexible_value is None:
        return False

    return str(flexible_value).lower() == "true"


def parse_annotations(annotations: dict[str, Any] | None) -> ParsedAnnotations:
    """Parse and validate job annotations.

    Args:
        annotations: Job metadata annotations

    Returns:
        ParsedAnnotations with validated values

    Raises:
        AnnotationError: If required annotations are missing or invalid
    """
    if annotations is None:
        raise AnnotationError("No annotations provided")

    flexible = is_flexible_job(annotations)
    if not flexible:
        raise AnnotationError(
            f"Job does not have {ANNOTATION_FLEXIBLE}='true' annotation",
            annotation_key=ANNOTATION_FLEXIBLE,
        )

    deadline_str = annotations.get(ANNOTATION_DEADLINE)
    if deadline_str is None:
        raise AnnotationError(
            f"Missing required annotation: {ANNOTATION_DEADLINE}",
            annotation_key=ANNOTATION_DEADLINE,
        )

    runtime_str = annotations.get(ANNOTATION_ESTIMATED_RUNTIME)
    if runtime_str is None:
        raise AnnotationError(
            f"Missing required annotation: {ANNOTATION_ESTIMATED_RUNTIME}",
            annotation_key=ANNOTATION_ESTIMATED_RUNTIME,
        )

    deadline = parse_iso8601_datetime(str(deadline_str))
    estimated_runtime = parse_duration(str(runtime_str))

    planned_start: datetime | None = None
    planned_start_str = annotations.get(ANNOTATION_PLANNED_START)
    if planned_start_str:
        with contextlib.suppress(AnnotationError):
            planned_start = parse_iso8601_datetime(str(planned_start_str))

    latest_start: datetime | None = None
    latest_start_str = annotations.get(ANNOTATION_LATEST_START)
    if latest_start_str:
        with contextlib.suppress(AnnotationError):
            latest_start = parse_iso8601_datetime(str(latest_start_str))

    planned_at: datetime | None = None
    planned_at_str = annotations.get(ANNOTATION_PLANNED_AT)
    if planned_at_str:
        with contextlib.suppress(AnnotationError):
            planned_at = parse_iso8601_datetime(str(planned_at_str))

    policy_used = annotations.get(ANNOTATION_POLICY_USED)
    fallback_reason = annotations.get(ANNOTATION_FALLBACK_REASON)

    policy_override: str | None = None
    policy_str = annotations.get(ANNOTATION_POLICY)
    if policy_str:
        policy_override = str(policy_str).strip().lower()
        if policy_override not in VALID_POLICIES:
            raise AnnotationError(
                f"Unknown policy '{policy_str}'. Valid policies: {', '.join(VALID_POLICIES)}",
                annotation_key=ANNOTATION_POLICY,
            )

    threshold_override: int | None = None
    threshold_str = annotations.get(ANNOTATION_RENEWABLE_THRESHOLD)
    if threshold_str is not None and str(threshold_str).strip():
        try:
            threshold_override = int(float(str(threshold_str).strip().rstrip("%")))
        except ValueError as e:
            raise AnnotationError(
                f"Cannot parse renewable threshold '{threshold_str}'. Expected 0-100",
                annotation_key=ANNOTATION_RENEWABLE_THRESHOLD,
            ) from e
        if not 0 <= threshold_override <= 100:
            raise AnnotationError(
                f"Renewable threshold must be between 0 and 100, got {threshold_override}",
                annotation_key=ANNOTATION_RENEWABLE_THRESHOLD,
            )

    max_delay: timedelta | None = None
    max_delay_str = annotations.get(ANNOTATION_MAX_DELAY)
    if max_delay_str:
        max_delay = parse_duration(str(max_delay_str), annotation_key=ANNOTATION_MAX_DELAY)

    return ParsedAnnotations(
        flexible=True,
        deadline=deadline,
        estimated_runtime=estimated_runtime,
        planned_start=planned_start,
        policy_used=str(policy_used) if policy_used else None,
        fallback_reason=str(fallback_reason) if fallback_reason else None,
        policy_override=policy_override,
        renewable_threshold_override=threshold_override,
        max_delay=max_delay,
        latest_start=latest_start,
        planned_at=planned_at,
    )


def validate_deadline(
    deadline: datetime,
    estimated_runtime: timedelta,
    safety_margin: timedelta,
    current_time: datetime | None = None,
) -> tuple[bool, str | None]:
    """Validate that the deadline is achievable.

    Args:
        deadline: Job deadline
        estimated_runtime: Estimated job runtime
        safety_margin: Safety margin before deadline
        current_time: Current time (defaults to now)

    Returns:
        Tuple of (is_valid, error_message)
    """
    if current_time is None:
        current_time = datetime.now(UTC)

    latest_feasible_start = deadline - estimated_runtime - safety_margin

    if current_time > latest_feasible_start:
        return (
            False,
            f"Deadline {deadline.isoformat()} is not achievable. "
            f"Latest feasible start ({latest_feasible_start.isoformat()}) "
            f"has already passed.",
        )

    if deadline <= current_time:
        return (False, f"Deadline {deadline.isoformat()} is in the past.")

    return (True, None)


def format_annotation_value(value: datetime | timedelta | str) -> str:
    """Format a value for use as an annotation.

    Args:
        value: Value to format

    Returns:
        String representation for annotation
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, timedelta):
        total_seconds = int(value.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)

        parts = []
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if seconds or not parts:
            parts.append(f"{seconds}s")
        return "".join(parts)
    return str(value)
