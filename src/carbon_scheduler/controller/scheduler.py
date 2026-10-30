"""Job scheduling logic for the carbon-aware scheduler.

This module contains the core scheduling logic including:
- Calculating planned start times using planner policies
- Determining when to release jobs
- Handling fail-open behavior when grid data is unavailable
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from carbon_scheduler.carbon.live import (
    estimate_avoided_emissions,
    extract_job_resources,
    mean_share_for_window,
)
from carbon_scheduler.config import (
    ANNOTATION_CO2_AVOIDED,
    ANNOTATION_EXPECTED_RENEWABLE_SHARE,
    ANNOTATION_FALLBACK_REASON,
    ANNOTATION_LATEST_START,
    ANNOTATION_PLANNED_AT,
    ANNOTATION_PLANNED_START,
    ANNOTATION_POLICY_USED,
    Settings,
    get_settings,
)
from carbon_scheduler.controller.annotations import (
    AnnotationError,
    ParsedAnnotations,
    parse_annotations,
    parse_duration,
)
from carbon_scheduler.grid import EnergyChartsClient, EnergyChartsError, GridData
from carbon_scheduler.metrics.prometheus import (
    record_fallback_release,
    record_planned_co2_avoided,
    record_replan_decision,
    track_held_job,
    update_forecast_age,
    update_renewable_share,
)
from carbon_scheduler.planner.models import Forecast, ForecastPoint, JobInfo, PolicyConfig
from carbon_scheduler.planner.policies import get_policy

logger = structlog.get_logger(__name__)


class ScheduleDecision:
    """Represents a scheduling decision for a job."""

    def __init__(
        self,
        should_release: bool,
        planned_start: datetime | None = None,
        policy_used: str | None = None,
        is_fallback: bool = False,
        fallback_reason: str | None = None,
        annotations_to_set: dict[str, str] | None = None,
    ):
        self.should_release = should_release
        self.planned_start = planned_start
        self.policy_used = policy_used
        self.is_fallback = is_fallback
        self.fallback_reason = fallback_reason
        self.annotations_to_set = annotations_to_set or {}

    @classmethod
    def release_now(
        cls,
        policy_used: str = "immediate",
        is_fallback: bool = False,
        fallback_reason: str | None = None,
    ) -> "ScheduleDecision":
        """Create a decision to release the job immediately."""
        now = datetime.now(UTC)
        annotations: dict[str, str] = {
            ANNOTATION_PLANNED_START: now.isoformat(),
            ANNOTATION_POLICY_USED: policy_used,
        }
        if fallback_reason:
            annotations[ANNOTATION_FALLBACK_REASON] = fallback_reason

        return cls(
            should_release=True,
            planned_start=now,
            policy_used=policy_used,
            is_fallback=is_fallback,
            fallback_reason=fallback_reason,
            annotations_to_set=annotations,
        )

    @classmethod
    def hold_until(
        cls,
        planned_start: datetime,
        policy_used: str,
        is_fallback: bool = False,
        fallback_reason: str | None = None,
        latest_start: datetime | None = None,
        expected_renewable_share: float | None = None,
        co2_avoided_grams: float | None = None,
        planned_at: datetime | None = None,
    ) -> "ScheduleDecision":
        """Create a decision to hold the job until the planned start."""
        annotations: dict[str, str] = {
            ANNOTATION_PLANNED_START: planned_start.isoformat(),
            ANNOTATION_POLICY_USED: policy_used,
        }
        if planned_at is not None:
            annotations[ANNOTATION_PLANNED_AT] = planned_at.isoformat()
        if fallback_reason:
            annotations[ANNOTATION_FALLBACK_REASON] = fallback_reason
        if latest_start is not None:
            annotations[ANNOTATION_LATEST_START] = latest_start.isoformat()
        if expected_renewable_share is not None:
            annotations[ANNOTATION_EXPECTED_RENEWABLE_SHARE] = f"{expected_renewable_share:.1f}"
        if co2_avoided_grams is not None:
            annotations[ANNOTATION_CO2_AVOIDED] = f"{co2_avoided_grams:.2f}"

        return cls(
            should_release=False,
            planned_start=planned_start,
            policy_used=policy_used,
            is_fallback=is_fallback,
            fallback_reason=fallback_reason,
            annotations_to_set=annotations,
        )


@dataclass(frozen=True)
class ReplanDecision:
    """Outcome of re-checking a held job against a newer forecast (F-20).

    Attributes:
        moved: True if the planned start changed.
        old_start: Planned start before the check.
        new_start: Planned start after the check (equal to ``old_start`` if kept).
        old_share_pct: Mean forecast share of the old window, under the new forecast.
        new_share_pct: Mean forecast share of the new window, under the new forecast.
        annotations_to_set: Annotations to patch onto the Job.
    """

    moved: bool
    old_start: datetime
    new_start: datetime
    old_share_pct: float | None
    new_share_pct: float | None
    annotations_to_set: dict[str, str]


def _as_utc(value: datetime) -> datetime:
    """Treat naive datetimes as local time and convert to UTC (like the grid cache)."""
    return value.astimezone(UTC)


class JobScheduler:
    """Core scheduler logic for carbon-aware job scheduling."""

    def __init__(
        self,
        grid_client: EnergyChartsClient | None = None,
        settings: Settings | None = None,
    ):
        self.settings = settings or get_settings()
        self._grid_client = grid_client
        self._policy_config = PolicyConfig(
            renewable_threshold_pct=self.settings.renewable_threshold_pct,
            safety_margin=timedelta(minutes=self.settings.deadline_safety_margin_minutes),
        )

    @property
    def grid_client(self) -> EnergyChartsClient | None:
        """Get the grid client."""
        return self._grid_client

    @grid_client.setter
    def grid_client(self, client: EnergyChartsClient) -> None:
        """Set the grid client."""
        self._grid_client = client

    def _global_max_delay(self) -> timedelta | None:
        """Parse the global max-delay setting; None disables the cap."""
        raw = (self.settings.max_delay or "").strip()
        if not raw or raw in ("0", "none", "off"):
            return None
        try:
            return parse_duration(raw)
        except AnnotationError:
            logger.warning("invalid_max_delay_setting", value=raw)
            return None

    def _record_grid_gauges(self, grid_data: GridData, current_time: datetime) -> None:
        """Expose the current renewable share and forecast age as metrics."""
        share = grid_data.get_renewable_share_at(int(current_time.timestamp()))
        if share is not None:
            update_renewable_share(share)
        fetched_at = grid_data.fetched_at
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.astimezone(UTC)
        update_forecast_age(max(0.0, (current_time - fetched_at).total_seconds()))

    def _grid_data_to_forecast(self, grid_data: GridData | None) -> Forecast | None:
        """Convert GridData to Forecast for the planner."""
        if grid_data is None or grid_data.forecast is None:
            return None

        forecast = grid_data.forecast
        if not forecast.unix_seconds or not forecast.ren_share:
            return None

        points = []
        for ts, share in zip(forecast.unix_seconds, forecast.ren_share, strict=False):
            points.append(
                ForecastPoint(
                    timestamp=datetime.fromtimestamp(ts, tz=UTC),
                    renewable_share_pct=share,
                )
            )

        return Forecast(points=points, degraded=forecast.substitute)

    async def get_grid_data(self) -> GridData | None:
        """Fetch current grid data.

        Returns:
            GridData if available, None otherwise
        """
        if self._grid_client is None:
            logger.warning("grid_client_not_configured")
            return None

        try:
            return await self._grid_client.get_grid_data()
        except EnergyChartsError as e:
            logger.error("grid_data_fetch_failed", error=str(e))
            return None
        except Exception as e:
            logger.error("grid_data_fetch_unexpected_error", error=str(e))
            return None

    async def calculate_schedule(
        self,
        job_name: str,
        job_namespace: str,
        annotations: dict[str, Any],
        current_time: datetime | None = None,
        job_spec: dict[str, Any] | None = None,
    ) -> ScheduleDecision:
        """Calculate the scheduling decision for a job.

        Args:
            job_name: Name of the Kubernetes job
            job_namespace: Namespace of the job
            annotations: Job annotations
            current_time: Current time (defaults to now)
            job_spec: Job spec, used to estimate energy from resource requests

        Returns:
            ScheduleDecision with the scheduling plan
        """
        if current_time is None:
            current_time = datetime.now(UTC)

        try:
            parsed = parse_annotations(annotations)
        except AnnotationError as e:
            logger.warning(
                "annotation_validation_failed",
                job_name=job_name,
                namespace=job_namespace,
                error=str(e),
            )
            return ScheduleDecision.release_now(
                policy_used="immediate",
                is_fallback=True,
                fallback_reason=f"validation_error: {e}",
            )

        policy_name = parsed.policy_override or self.settings.policy
        threshold_pct = (
            parsed.renewable_threshold_override
            if parsed.renewable_threshold_override is not None
            else self.settings.renewable_threshold_pct
        )

        safety_margin = timedelta(minutes=self.settings.deadline_safety_margin_minutes)

        # A max-delay cap tightens the planning horizon: the job must start no later
        # than submit + max_delay, which is modelled as an earlier effective deadline.
        effective_deadline = parsed.deadline
        max_delay = parsed.max_delay or self._global_max_delay()
        if max_delay is not None:
            capped = current_time + max_delay + parsed.estimated_runtime + safety_margin
            effective_deadline = min(effective_deadline, capped)

        job_info = JobInfo(
            name=job_name,
            namespace=job_namespace,
            submit_time=current_time,
            deadline=effective_deadline,
            estimated_runtime=parsed.estimated_runtime,
        )

        latest_feasible_start = job_info.latest_feasible_start(safety_margin)

        if current_time >= latest_feasible_start:
            logger.info(
                "job_past_latest_feasible_start",
                job_name=job_name,
                namespace=job_namespace,
                latest_feasible_start=latest_feasible_start.isoformat(),
            )
            return ScheduleDecision.release_now(
                policy_used=policy_name,
                is_fallback=True,
                fallback_reason="current_time_past_latest_feasible_start",
            )

        grid_data = await self.get_grid_data()

        if grid_data is None:
            logger.warning(
                "grid_data_unavailable_releasing_job",
                job_name=job_name,
                namespace=job_namespace,
            )
            record_fallback_release(job_namespace, job_name)
            return ScheduleDecision.release_now(
                policy_used=policy_name,
                is_fallback=True,
                fallback_reason="grid_data_unavailable",
            )

        self._record_grid_gauges(grid_data, current_time)
        forecast = self._grid_data_to_forecast(grid_data)

        if forecast is None or grid_data.is_degraded:
            logger.warning(
                "forecast_data_degraded_or_missing",
                job_name=job_name,
                namespace=job_namespace,
                is_degraded=grid_data.is_degraded if grid_data else None,
            )
            record_fallback_release(job_namespace, job_name)
            return ScheduleDecision.release_now(
                policy_used=policy_name,
                is_fallback=True,
                fallback_reason="forecast_data_degraded" if grid_data.is_degraded else "no_forecast_data",
            )

        policy = get_policy(
            policy_name,  # type: ignore[arg-type]  # validated by parse_annotations/Settings
            config=self._policy_config,
            threshold_pct=threshold_pct,
        )

        window = policy.select(job_info, forecast, current_time)

        if window.is_fallback:
            logger.info(
                "policy_returned_fallback",
                job_name=job_name,
                namespace=job_namespace,
                policy=policy_name,
                fallback_reason=window.fallback_reason,
            )
            record_fallback_release(job_namespace, job_name)
            return ScheduleDecision.release_now(
                policy_used=window.policy_used,
                is_fallback=True,
                fallback_reason=window.fallback_reason,
            )

        if window.start <= current_time:
            return ScheduleDecision.release_now(
                policy_used=window.policy_used,
                is_fallback=False,
            )

        avoided = estimate_avoided_emissions(
            forecast=forecast,
            submit_time=current_time,
            planned_start=window.start,
            runtime=parsed.estimated_runtime,
            resources=extract_job_resources(job_spec),
            fossil_intensity_gco2_kwh=self.settings.fossil_intensity_gco2_kwh,
            watts_per_cpu_core=self.settings.watts_per_cpu_core,
            watts_per_gpu=self.settings.watts_per_gpu,
        )

        logger.info(
            "job_scheduled",
            job_name=job_name,
            namespace=job_namespace,
            planned_start=window.start.isoformat(),
            latest_start=latest_feasible_start.isoformat(),
            policy=window.policy_used,
            mean_renewable_share=window.mean_renewable_share_pct,
            estimated_co2_avoided_grams=avoided.avoided_gco2 if avoided else None,
        )

        track_held_job(job_namespace, job_name)
        if avoided is not None:
            record_planned_co2_avoided(job_namespace, avoided.avoided_gco2)

        return ScheduleDecision.hold_until(
            planned_start=window.start,
            policy_used=window.policy_used,
            latest_start=latest_feasible_start,
            expected_renewable_share=window.mean_renewable_share_pct,
            co2_avoided_grams=avoided.avoided_gco2 if avoided else None,
            planned_at=current_time,
        )

    async def replan(
        self,
        job_name: str,
        job_namespace: str,
        parsed: ParsedAnnotations,
        current_time: datetime | None = None,
        job_spec: dict[str, Any] | None = None,
        submit_time: datetime | None = None,
    ) -> ReplanDecision | None:
        """Re-check a held job's plan when a newer forecast is available (F-20).

        The policy is re-run from ``current_time`` with the recorded ``latest-start``
        as the hard limit. The plan moves only when the new window's mean renewable
        share beats the current window's share (both under the new forecast) by more
        than ``replan_min_improvement_pct``. ``planned-at`` is advanced on every check,
        so each forecast refresh triggers at most one re-plan.

        Args:
            job_name: Name of the Kubernetes job
            job_namespace: Namespace of the job
            parsed: Parsed annotations of the held job
            current_time: Current time (defaults to now)
            job_spec: Job spec, used to re-estimate CO2 avoided
            submit_time: Job creation time, baseline for the CO2 estimate

        Returns:
            A ReplanDecision, or None if no check was due or possible.
        """
        if not self.settings.replan_enabled or parsed.planned_start is None:
            return None
        if current_time is None:
            current_time = datetime.now(UTC)

        safety_margin = timedelta(minutes=self.settings.deadline_safety_margin_minutes)
        latest_start = parsed.latest_start or (
            parsed.deadline - parsed.estimated_runtime - safety_margin
        )
        if current_time >= parsed.planned_start or current_time >= latest_start:
            return None  # due for release; the release path handles it

        policy_name = parsed.policy_override or self.settings.policy
        if policy_name == "immediate":
            return None

        grid_data = await self.get_grid_data()
        if grid_data is None or grid_data.is_degraded:
            return None  # keep the existing plan; deadlines are still guarded
        fetched_at = _as_utc(grid_data.fetched_at)
        if parsed.planned_at is not None and fetched_at <= _as_utc(parsed.planned_at):
            return None  # this forecast was already used for the current plan
        forecast = self._grid_data_to_forecast(grid_data)
        if forecast is None:
            return None

        threshold_pct = (
            parsed.renewable_threshold_override
            if parsed.renewable_threshold_override is not None
            else self.settings.renewable_threshold_pct
        )
        policy = get_policy(
            policy_name,  # type: ignore[arg-type]  # validated by parse_annotations/Settings
            config=self._policy_config,
            threshold_pct=threshold_pct,
        )
        # Model latest-start as the deadline so the policy can never pick a later start.
        job_info = JobInfo(
            name=job_name,
            namespace=job_namespace,
            submit_time=current_time,
            deadline=latest_start + parsed.estimated_runtime + safety_margin,
            estimated_runtime=parsed.estimated_runtime,
        )
        window = policy.select(job_info, forecast, current_time)

        runtime = parsed.estimated_runtime
        old_start = parsed.planned_start
        old_share = mean_share_for_window(forecast, old_start, runtime)
        new_start = max(window.start, current_time)
        new_share = mean_share_for_window(forecast, new_start, runtime)

        margin = self.settings.replan_min_improvement_pct
        moved = (
            not window.is_fallback
            and new_start <= latest_start
            and new_start != old_start
            and new_share is not None
            and (old_share is None or new_share - old_share > margin)
        )

        annotations = {ANNOTATION_PLANNED_AT: current_time.isoformat()}
        if moved:
            annotations[ANNOTATION_PLANNED_START] = new_start.isoformat()
            annotations[ANNOTATION_POLICY_USED] = window.policy_used
            if new_share is not None:
                annotations[ANNOTATION_EXPECTED_RENEWABLE_SHARE] = f"{new_share:.1f}"
            avoided = estimate_avoided_emissions(
                forecast=forecast,
                submit_time=submit_time or current_time,
                planned_start=new_start,
                runtime=runtime,
                resources=extract_job_resources(job_spec),
                fossil_intensity_gco2_kwh=self.settings.fossil_intensity_gco2_kwh,
                watts_per_cpu_core=self.settings.watts_per_cpu_core,
                watts_per_gpu=self.settings.watts_per_gpu,
            )
            if avoided is not None:
                annotations[ANNOTATION_CO2_AVOIDED] = f"{avoided.avoided_gco2:.2f}"
        else:
            new_start = old_start

        record_replan_decision(job_namespace, moved)
        logger.info(
            "job_replan_checked",
            job_name=job_name,
            namespace=job_namespace,
            moved=moved,
            old_start=old_start.isoformat(),
            new_start=new_start.isoformat(),
            old_share=old_share,
            new_share=new_share if moved else old_share,
            min_improvement_pct=margin,
        )
        return ReplanDecision(
            moved=moved,
            old_start=old_start,
            new_start=new_start,
            old_share_pct=old_share,
            new_share_pct=new_share if moved else old_share,
            annotations_to_set=annotations,
        )

    def should_release_now(
        self,
        parsed: ParsedAnnotations,
        current_time: datetime | None = None,
    ) -> bool:
        """Check if a held job should be released now.

        A job should be released if:
        1. The planned start time has been reached (within 60s tolerance)
        2. The recorded latest start (deadline or max-delay cap) has been reached
        3. The deadline minus runtime minus safety margin has been reached

        Args:
            parsed: Parsed job annotations
            current_time: Current time (defaults to now)

        Returns:
            True if the job should be released
        """
        if current_time is None:
            current_time = datetime.now(UTC)

        if parsed.planned_start is not None and current_time >= parsed.planned_start:
            return True

        if parsed.latest_start is not None and current_time >= parsed.latest_start:
            return True

        safety_margin = timedelta(minutes=self.settings.deadline_safety_margin_minutes)
        latest_feasible_start = parsed.deadline - parsed.estimated_runtime - safety_margin

        return current_time >= latest_feasible_start

    def calculate_delay_seconds(
        self,
        submit_time: datetime,
        release_time: datetime | None = None,
    ) -> float:
        """Calculate the delay in seconds from submission to release.

        Args:
            submit_time: When the job was submitted
            release_time: When the job was released (defaults to now)

        Returns:
            Delay in seconds
        """
        if release_time is None:
            release_time = datetime.now(UTC)

        return (release_time - submit_time).total_seconds()
