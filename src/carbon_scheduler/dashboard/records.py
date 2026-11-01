"""Build dashboard :class:`JobRecord` objects from Job annotations."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Any

from carbon_scheduler.config import (
    ANNOTATION_CO2_AVOIDED,
    ANNOTATION_DEADLINE,
    ANNOTATION_ESTIMATED_RUNTIME,
    ANNOTATION_EXPECTED_RENEWABLE_SHARE,
    ANNOTATION_FALLBACK_REASON,
    ANNOTATION_LATEST_START,
    ANNOTATION_PLANNED_START,
    ANNOTATION_POLICY_USED,
)
from carbon_scheduler.dashboard.state import JobRecord, JobStatus
from carbon_scheduler.planner.models import JobInfo

TEAM_LABELS = ("team", "app.kubernetes.io/part-of", "owner")


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def record_from_annotations(
    namespace: str,
    name: str,
    annotations: dict[str, Any],
    status: JobStatus,
    submitted: datetime | None = None,
    labels: dict[str, Any] | None = None,
) -> JobRecord:
    """Translate controller annotations into a dashboard record."""
    runtime: float | None = None
    raw_runtime = annotations.get(ANNOTATION_ESTIMATED_RUNTIME)
    if raw_runtime:
        with contextlib.suppress(ValueError):
            runtime = JobInfo._parse_duration_string(str(raw_runtime)).total_seconds()

    team = None
    for key in TEAM_LABELS:
        if labels and labels.get(key):
            team = str(labels[key])
            break

    return JobRecord(
        namespace=namespace,
        name=name,
        status=status,
        policy=annotations.get(ANNOTATION_POLICY_USED),
        submitted=submitted,
        planned_start=_dt(annotations.get(ANNOTATION_PLANNED_START)),
        latest_start=_dt(annotations.get(ANNOTATION_LATEST_START)),
        deadline=_dt(annotations.get(ANNOTATION_DEADLINE)),
        runtime_seconds=runtime,
        expected_share_pct=_float(annotations.get(ANNOTATION_EXPECTED_RENEWABLE_SHARE)),
        co2_avoided_g=_float(annotations.get(ANNOTATION_CO2_AVOIDED)),
        fallback_reason=annotations.get(ANNOTATION_FALLBACK_REASON),
        team=team,
    )
