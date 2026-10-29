"""Kopf event handlers for the carbon-aware Kubernetes scheduler.

This module implements the Kubernetes controller using kopf:
- F-02: Suspend and release controller
- F-05: Deadline guarantee and fail-open behavior
- F-12: Kubernetes Events for state changes
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import kopf
import structlog

from carbon_scheduler.config import (
    ANNOTATION_ACTUAL_START,
    ANNOTATION_PLANNED_START,
    Settings,
    get_settings,
)
from carbon_scheduler.controller.annotations import (
    AnnotationError,
    is_flexible_job,
    parse_annotations,
)
from carbon_scheduler.controller.scheduler import JobScheduler
from carbon_scheduler.grid import EnergyChartsClient
from carbon_scheduler.metrics.prometheus import (
    record_fallback_release,
    record_job_released,
)

logger = structlog.get_logger(__name__)

_scheduler: JobScheduler | None = None
_settings: Settings | None = None


def get_scheduler() -> JobScheduler:
    """Get or create the global scheduler instance."""
    global _scheduler, _settings
    if _scheduler is None:
        _settings = get_settings()
        _scheduler = JobScheduler(settings=_settings)
    return _scheduler


def set_scheduler(scheduler: JobScheduler) -> None:
    """Set the global scheduler instance (for testing)."""
    global _scheduler
    _scheduler = scheduler


async def emit_event(
    name: str,
    namespace: str,
    reason: str,
    message: str,
    event_type: str = "Normal",
) -> None:
    """Emit a Kubernetes Event for a job.

    Args:
        name: Job name
        namespace: Job namespace
        reason: Event reason code (e.g., JobHeld, JobReleased)
        message: Human-readable event message
        event_type: Event type (Normal or Warning)
    """
    logger.info(
        "kubernetes_event",
        job_name=name,
        namespace=namespace,
        reason=reason,
        message=message,
        event_type=event_type,
    )


def should_process_job(spec: dict[str, Any], meta: dict[str, Any]) -> bool:
    """Check if a job should be processed by the carbon scheduler.

    A job should be processed if:
    1. It has spec.suspend=true
    2. It has the flexible annotation set to "true"

    Args:
        spec: Job spec
        meta: Job metadata

    Returns:
        True if the job should be processed
    """
    suspend = spec.get("suspend", False)
    if not suspend:
        return False

    annotations = meta.get("annotations", {})
    return is_flexible_job(annotations)


async def handle_job_create(
    spec: dict[str, Any],
    meta: dict[str, Any],
    name: str,
    namespace: str,
    patch: dict[str, Any],
    **_kwargs: Any,
) -> dict[str, Any] | None:
    """Handle job creation events.

    This is the core handler for F-02 (Suspend and Release Controller).

    Args:
        spec: Job spec
        meta: Job metadata
        name: Job name
        namespace: Job namespace
        patch: Patch object for modifying the job

    Returns:
        Status dict for kopf memo, or None if job is not processed
    """
    if not should_process_job(spec, meta):
        logger.debug(
            "job_ignored_not_flexible",
            job_name=name,
            namespace=namespace,
        )
        return None

    annotations = meta.get("annotations", {})

    try:
        parse_annotations(annotations)
    except AnnotationError as e:
        await emit_event(
            name=name,
            namespace=namespace,
            reason="ValidationError",
            message=f"Invalid job annotations: {e}",
            event_type="Warning",
        )

        patch.setdefault("spec", {})["suspend"] = False

        logger.warning(
            "job_validation_failed_releasing",
            job_name=name,
            namespace=namespace,
            error=str(e),
        )
        return {"released": True, "fallback": True, "reason": str(e)}

    scheduler = get_scheduler()

    decision = await scheduler.calculate_schedule(
        job_name=name,
        job_namespace=namespace,
        annotations=annotations,
    )

    if decision.annotations_to_set:
        patch.setdefault("metadata", {}).setdefault("annotations", {}).update(
            decision.annotations_to_set
        )

    if decision.should_release:
        patch.setdefault("spec", {})["suspend"] = False

        if decision.is_fallback:
            await emit_event(
                name=name,
                namespace=namespace,
                reason="FallbackRelease",
                message=f"Job released immediately due to: {decision.fallback_reason}",
                event_type="Warning",
            )
        else:
            await emit_event(
                name=name,
                namespace=namespace,
                reason="JobReleased",
                message=f"Job released for immediate execution (policy: {decision.policy_used})",
            )

        logger.info(
            "job_released",
            job_name=name,
            namespace=namespace,
            policy=decision.policy_used,
            is_fallback=decision.is_fallback,
        )

        return {
            "released": True,
            "fallback": decision.is_fallback,
            "policy": decision.policy_used,
        }

    await emit_event(
        name=name,
        namespace=namespace,
        reason="JobHeld",
        message=f"Job held until {decision.planned_start.isoformat() if decision.planned_start else 'unknown'} (policy: {decision.policy_used})",
    )

    await emit_event(
        name=name,
        namespace=namespace,
        reason="JobPlanned",
        message=f"Planned start: {decision.planned_start.isoformat() if decision.planned_start else 'unknown'}",
    )

    logger.info(
        "job_held",
        job_name=name,
        namespace=namespace,
        planned_start=decision.planned_start.isoformat() if decision.planned_start else None,
        policy=decision.policy_used,
    )

    return {
        "released": False,
        "planned_start": decision.planned_start.isoformat() if decision.planned_start else None,
        "policy": decision.policy_used,
    }


async def handle_job_timer(
    spec: dict[str, Any],
    meta: dict[str, Any],
    name: str,
    namespace: str,
    patch: dict[str, Any],
    **_kwargs: Any,
) -> dict[str, Any] | None:
    """Periodic timer handler to check if jobs should be released.

    This runs every interval (default 60s) to check if held jobs
    should now be released based on their planned start time.

    Args:
        spec: Job spec
        meta: Job metadata
        name: Job name
        namespace: Job namespace
        patch: Patch object for modifying the job

    Returns:
        Updated status dict, or None if no action taken
    """
    suspend = spec.get("suspend", False)
    if not suspend:
        return None

    annotations = meta.get("annotations", {})
    if not is_flexible_job(annotations):
        return None

    try:
        parsed = parse_annotations(annotations)
    except AnnotationError:
        return None

    scheduler = get_scheduler()
    current_time = datetime.now(UTC)

    if scheduler.should_release_now(parsed, current_time):
        patch.setdefault("spec", {})["suspend"] = False

        now_iso = current_time.isoformat()
        patch.setdefault("metadata", {}).setdefault("annotations", {})[
            ANNOTATION_ACTUAL_START
        ] = now_iso

        is_fallback = parsed.planned_start is None or current_time > (
            parsed.planned_start + timedelta(seconds=60)
        )

        if is_fallback:
            await emit_event(
                name=name,
                namespace=namespace,
                reason="FallbackRelease",
                message="Job released at deadline safety margin",
                event_type="Warning",
            )
            record_fallback_release(namespace, name)
        else:
            await emit_event(
                name=name,
                namespace=namespace,
                reason="JobReleased",
                message="Job released at planned start time",
            )

        record_job_released(namespace, name)

        logger.info(
            "timer_released_job",
            job_name=name,
            namespace=namespace,
            planned_start=parsed.planned_start.isoformat() if parsed.planned_start else None,
            actual_release=now_iso,
            is_fallback=is_fallback,
        )

        return {
            "released": True,
            "release_time": now_iso,
            "fallback": is_fallback,
        }

    return None


@kopf.on.create("batch", "v1", "jobs")
async def on_job_create(
    spec: dict[str, Any],
    meta: dict[str, Any],
    name: str,
    namespace: str,
    patch: kopf.Patch,
    **_kwargs: Any,
) -> dict[str, Any] | None:
    """Kopf handler for job creation events."""
    return await handle_job_create(
        spec=spec,
        meta=meta,
        name=name,
        namespace=namespace,
        patch=patch,
    )


@kopf.on.update("batch", "v1", "jobs")
async def on_job_update(
    spec: dict[str, Any],
    meta: dict[str, Any],
    name: str,
    namespace: str,
    patch: kopf.Patch,
    **_kwargs: Any,
) -> dict[str, Any] | None:
    """Kopf handler for job update events.

    This handles the case where a job is created without being suspended,
    then later gets suspended with the flexible annotation.
    """
    if not should_process_job(spec, meta):
        return None

    annotations = meta.get("annotations", {})
    planned_start_str = annotations.get(ANNOTATION_PLANNED_START)

    if planned_start_str:
        return None

    return await handle_job_create(
        spec=spec,
        meta=meta,
        name=name,
        namespace=namespace,
        patch=patch,
    )


@kopf.timer("batch", "v1", "jobs", interval=60.0)
async def check_release_timer(
    spec: dict[str, Any],
    meta: dict[str, Any],
    name: str,
    namespace: str,
    patch: kopf.Patch,
    **_kwargs: Any,
) -> dict[str, Any] | None:
    """Kopf timer handler to check for job release."""
    return await handle_job_timer(
        spec=spec,
        meta=meta,
        name=name,
        namespace=namespace,
        patch=patch,
    )


@kopf.on.startup()
async def on_startup(**_kwargs: Any) -> None:
    """Kopf startup handler to initialize the controller."""
    settings = get_settings()

    logger.info(
        "controller_starting",
        policy=settings.policy,
        grid_country=settings.grid_country,
        safety_margin_minutes=settings.deadline_safety_margin_minutes,
        reconcile_interval_seconds=settings.reconcile_interval_seconds,
    )

    scheduler = get_scheduler()

    grid_client = EnergyChartsClient(
        base_url=settings.grid_api_base_url,
        country=settings.grid_country,
    )

    scheduler.grid_client = grid_client

    logger.info("controller_initialized")


@kopf.on.cleanup()
async def on_cleanup(**_kwargs: Any) -> None:
    """Kopf cleanup handler to shut down gracefully."""
    global _scheduler

    if _scheduler and _scheduler.grid_client:
        logger.info("closing_grid_client")

    _scheduler = None
    logger.info("controller_stopped")
