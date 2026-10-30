"""Kopf event handlers for the carbon-aware Kubernetes scheduler.

This module implements the Kubernetes controller using kopf:
- F-02: Suspend and release controller
- F-05: Deadline guarantee and fail-open behavior
- F-12: Kubernetes Events for state changes
- F-20: Re-plan held jobs when the forecast is refreshed
"""

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any

import kopf
import structlog

from carbon_scheduler.config import (
    ANNOTATION_ACTUAL_START,
    ANNOTATION_CO2_AVOIDED,
    ANNOTATION_PLANNED_START,
    Settings,
    get_settings,
)
from carbon_scheduler.controller.annotations import (
    AnnotationError,
    is_flexible_job,
    parse_annotations,
    parse_iso8601_datetime,
)
from carbon_scheduler.controller.scheduler import JobScheduler
from carbon_scheduler.grid import EnergyChartsClient
from carbon_scheduler.metrics.prometheus import (
    record_co2_avoided,
    record_fallback_release,
    record_job_released,
    set_ready,
    track_held_job,
    untrack_held_job,
)

logger = structlog.get_logger(__name__)

_scheduler: JobScheduler | None = None
_settings: Settings | None = None

# Read once at import: kopf needs the timer interval when the handler is registered.
RELEASE_TIMER_INTERVAL = float(get_settings().reconcile_interval_seconds)


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
    body: Any = None,
) -> None:
    """Emit a Kubernetes Event for a job and log it.

    The Event is posted through kopf when the Job body is available and the
    call happens inside a running operator; otherwise it is only logged.

    Args:
        name: Job name
        namespace: Job namespace
        reason: Event reason code (e.g., JobHeld, JobReleased)
        message: Human-readable event message
        event_type: Event type (Normal or Warning)
        body: Job body the Event refers to
    """
    logger.info(
        "kubernetes_event",
        job_name=name,
        namespace=namespace,
        reason=reason,
        message=message,
        event_type=event_type,
    )

    if body is None:
        return

    try:
        kopf.event(body, type=event_type, reason=reason, message=message)
    except LookupError:
        # Not running inside a kopf operator context (e.g. unit tests or CLI use).
        pass
    except Exception as e:  # Events are best-effort and must never block a release
        logger.warning("kubernetes_event_failed", job_name=name, reason=reason, error=str(e))


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


def _submit_time(meta: dict[str, Any]) -> datetime | None:
    """Return the Job's creation timestamp, if present and parseable."""
    created = meta.get("creationTimestamp")
    if not created:
        return None
    try:
        return parse_iso8601_datetime(str(created))
    except AnnotationError:
        return None


def _fmt_share(share: float | None) -> str:
    """Format a renewable share for Event messages."""
    return "n/a" if share is None else f"{share:.1f}%"


def _delay_seconds(meta: dict[str, Any], release_time: datetime) -> float | None:
    """Seconds between Job creation and release, if the creation time is known."""
    submitted = _submit_time(meta)
    if submitted is None:
        return None
    return max(0.0, (release_time - submitted).total_seconds())


async def handle_job_create(
    spec: dict[str, Any],
    meta: dict[str, Any],
    name: str,
    namespace: str,
    patch: dict[str, Any],
    body: Any = None,
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
        body: Full Job body, used to post Kubernetes Events

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
    now = datetime.now(UTC)

    try:
        parse_annotations(annotations)
    except AnnotationError as e:
        await emit_event(
            name=name,
            namespace=namespace,
            reason="ValidationError",
            message=f"Invalid job annotations: {e}",
            event_type="Warning",
            body=body,
        )

        patch.setdefault("spec", {})["suspend"] = False
        patch.setdefault("metadata", {}).setdefault("annotations", {})[
            ANNOTATION_ACTUAL_START
        ] = now.isoformat()
        record_fallback_release(namespace, name)
        record_job_released(namespace, name, adjust_held=False)

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
        job_spec=dict(spec),
    )

    if decision.annotations_to_set:
        patch.setdefault("metadata", {}).setdefault("annotations", {}).update(
            decision.annotations_to_set
        )

    if decision.should_release:
        patch.setdefault("spec", {})["suspend"] = False
        patch.setdefault("metadata", {}).setdefault("annotations", {})[
            ANNOTATION_ACTUAL_START
        ] = now.isoformat()
        record_job_released(
            namespace,
            name,
            delay_seconds=_delay_seconds(meta, now),
            adjust_held=False,
        )

        if decision.is_fallback:
            await emit_event(
                name=name,
                namespace=namespace,
                reason="FallbackRelease",
                message=f"Job released immediately due to: {decision.fallback_reason}",
                event_type="Warning",
                body=body,
            )
        else:
            await emit_event(
                name=name,
                namespace=namespace,
                reason="JobReleased",
                message=f"Job released for immediate execution (policy: {decision.policy_used})",
                body=body,
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

    planned = decision.planned_start.isoformat() if decision.planned_start else "unknown"
    co2 = decision.annotations_to_set.get(ANNOTATION_CO2_AVOIDED)
    co2_note = f", est. {co2} gCO2 avoided" if co2 else ""

    await emit_event(
        name=name,
        namespace=namespace,
        reason="JobHeld",
        message=f"Job held until {planned} (policy: {decision.policy_used}{co2_note})",
        body=body,
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
    body: Any = None,
    **_kwargs: Any,
) -> dict[str, Any] | None:
    """Periodic timer handler to check if jobs should be released.

    This runs every interval (default 60s) to check if held jobs
    should now be released based on their planned start time. It also
    rebuilds the held-jobs gauge after a controller restart.

    Args:
        spec: Job spec
        meta: Job metadata
        name: Job name
        namespace: Job namespace
        patch: Patch object for modifying the job
        body: Full Job body, used to post Kubernetes Events

    Returns:
        Updated status dict, or None if no action taken
    """
    annotations = meta.get("annotations", {})
    if not is_flexible_job(annotations):
        return None

    suspend = spec.get("suspend", False)
    if not suspend:
        # Released by us earlier or resumed by someone else
        untrack_held_job(namespace, name)
        return None

    try:
        parsed = parse_annotations(annotations)
    except AnnotationError:
        return None

    scheduler = get_scheduler()
    current_time = datetime.now(UTC)

    replan = await scheduler.replan(
        job_name=name,
        job_namespace=namespace,
        parsed=parsed,
        current_time=current_time,
        job_spec=spec,
        submit_time=_submit_time(meta),
    )
    if replan is not None:
        patch.setdefault("metadata", {}).setdefault("annotations", {}).update(
            replan.annotations_to_set
        )
        if replan.moved:
            parsed = dataclasses.replace(parsed, planned_start=replan.new_start)
            annotations = {**annotations, **replan.annotations_to_set}
            await emit_event(
                name=name,
                namespace=namespace,
                reason="JobReplanned",
                message=(
                    f"Planned start moved {replan.old_start.isoformat()} → "
                    f"{replan.new_start.isoformat()} after forecast refresh "
                    f"(renewable share {_fmt_share(replan.old_share_pct)} → "
                    f"{_fmt_share(replan.new_share_pct)})"
                ),
                body=body,
            )

    if not scheduler.should_release_now(parsed, current_time):
        if parsed.planned_start is not None:
            track_held_job(namespace, name)
        return None

    patch.setdefault("spec", {})["suspend"] = False

    now_iso = current_time.isoformat()
    patch.setdefault("metadata", {}).setdefault("annotations", {})[
        ANNOTATION_ACTUAL_START
    ] = now_iso

    is_fallback = parsed.planned_start is None or current_time > (
        parsed.planned_start + timedelta(seconds=60)
    )

    untrack_held_job(namespace, name)
    record_job_released(
        namespace,
        name,
        delay_seconds=_delay_seconds(meta, current_time),
        adjust_held=False,
    )

    if is_fallback:
        await emit_event(
            name=name,
            namespace=namespace,
            reason="FallbackRelease",
            message="Job released at deadline safety margin",
            event_type="Warning",
            body=body,
        )
        record_fallback_release(namespace, name)
    else:
        co2_str = annotations.get(ANNOTATION_CO2_AVOIDED)
        if co2_str:
            try:
                co2 = float(co2_str)
            except ValueError:
                co2 = 0.0
            if co2 > 0:
                record_co2_avoided(namespace, name, co2)
        await emit_event(
            name=name,
            namespace=namespace,
            reason="JobReleased",
            message="Job released at planned start time",
            body=body,
        )

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


@kopf.on.create("batch", "v1", "jobs")
async def on_job_create(
    spec: kopf.Spec,
    meta: kopf.Meta,
    name: str,
    namespace: str | None,
    patch: kopf.Patch,
    body: kopf.Body,
    **_kwargs: Any,
) -> dict[str, Any] | None:
    """Kopf handler for job creation events."""
    return await handle_job_create(
        spec=dict(spec),
        meta=dict(meta),
        name=name,
        namespace=namespace or "default",
        patch=patch,
        body=body,
    )


@kopf.on.update("batch", "v1", "jobs")
async def on_job_update(
    spec: kopf.Spec,
    meta: kopf.Meta,
    name: str,
    namespace: str | None,
    patch: kopf.Patch,
    body: kopf.Body,
    **_kwargs: Any,
) -> dict[str, Any] | None:
    """Kopf handler for job update events.

    This handles the case where a job is created without being suspended,
    then later gets suspended with the flexible annotation.
    """
    spec_dict = dict(spec)
    meta_dict = dict(meta)
    if not should_process_job(spec_dict, meta_dict):
        return None

    annotations = meta_dict.get("annotations", {})
    planned_start_str = annotations.get(ANNOTATION_PLANNED_START)

    if planned_start_str:
        return None

    return await handle_job_create(
        spec=spec_dict,
        meta=meta_dict,
        name=name,
        namespace=namespace or "default",
        patch=patch,
        body=body,
    )


@kopf.timer("batch", "v1", "jobs", interval=RELEASE_TIMER_INTERVAL)
async def check_release_timer(
    spec: kopf.Spec,
    meta: kopf.Meta,
    name: str,
    namespace: str | None,
    patch: kopf.Patch,
    body: kopf.Body,
    **_kwargs: Any,
) -> dict[str, Any] | None:
    """Kopf timer handler to check for job release."""
    return await handle_job_timer(
        spec=dict(spec),
        meta=dict(meta),
        name=name,
        namespace=namespace or "default",
        patch=patch,
        body=body,
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
        max_delay=settings.max_delay,
    )

    scheduler = get_scheduler()

    grid_client = EnergyChartsClient(
        base_url=settings.grid_api_base_url,
        country=settings.grid_country,
        cache_ttl=settings.forecast_refresh_minutes * 60,
    )
    # Open the HTTP session; without it every fetch fails and all jobs fall back.
    await grid_client.__aenter__()

    scheduler.grid_client = grid_client
    set_ready(True)

    logger.info("controller_initialized")


@kopf.on.cleanup()
async def on_cleanup(**_kwargs: Any) -> None:
    """Kopf cleanup handler to shut down gracefully."""
    global _scheduler

    set_ready(False)

    if _scheduler and _scheduler.grid_client:
        logger.info("closing_grid_client")
        await _scheduler.grid_client.__aexit__(None, None, None)

    _scheduler = None
    logger.info("controller_stopped")
