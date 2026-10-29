"""Job scheduling logic for the carbon-aware scheduler.

This module contains the core scheduling logic including:
- Calculating planned start times using planner policies
- Determining when to release jobs
- Handling fail-open behavior when grid data is unavailable
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from carbon_scheduler.config import (
    ANNOTATION_FALLBACK_REASON,
    ANNOTATION_PLANNED_START,
    ANNOTATION_POLICY_USED,
    Settings,
    get_settings,
)
from carbon_scheduler.controller.annotations import (
    AnnotationError,
    ParsedAnnotations,
    parse_annotations,
)
from carbon_scheduler.grid import EnergyChartsClient, EnergyChartsError, GridData
from carbon_scheduler.metrics.prometheus import (
    record_fallback_release,
    record_job_held,
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
    ) -> "ScheduleDecision":
        """Create a decision to hold the job until the planned start."""
        annotations: dict[str, str] = {
            ANNOTATION_PLANNED_START: planned_start.isoformat(),
            ANNOTATION_POLICY_USED: policy_used,
        }
        if fallback_reason:
            annotations[ANNOTATION_FALLBACK_REASON] = fallback_reason

        return cls(
            should_release=False,
            planned_start=planned_start,
            policy_used=policy_used,
            is_fallback=is_fallback,
            fallback_reason=fallback_reason,
            annotations_to_set=annotations,
        )


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
    ) -> ScheduleDecision:
        """Calculate the scheduling decision for a job.

        Args:
            job_name: Name of the Kubernetes job
            job_namespace: Namespace of the job
            annotations: Job annotations
            current_time: Current time (defaults to now)

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

        job_info = JobInfo(
            name=job_name,
            namespace=job_namespace,
            submit_time=current_time,
            deadline=parsed.deadline,
            estimated_runtime=parsed.estimated_runtime,
        )

        safety_margin = timedelta(minutes=self.settings.deadline_safety_margin_minutes)
        latest_feasible_start = job_info.latest_feasible_start(safety_margin)

        if current_time >= latest_feasible_start:
            logger.info(
                "job_past_latest_feasible_start",
                job_name=job_name,
                namespace=job_namespace,
                latest_feasible_start=latest_feasible_start.isoformat(),
            )
            return ScheduleDecision.release_now(
                policy_used=self.settings.policy,
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
                policy_used=self.settings.policy,
                is_fallback=True,
                fallback_reason="grid_data_unavailable",
            )

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
                policy_used=self.settings.policy,
                is_fallback=True,
                fallback_reason="forecast_data_degraded" if grid_data.is_degraded else "no_forecast_data",
            )

        policy = get_policy(
            self.settings.policy,
            config=self._policy_config,
            threshold_pct=self.settings.renewable_threshold_pct,
        )

        window = policy.select(job_info, forecast, current_time)

        if window.is_fallback:
            logger.info(
                "policy_returned_fallback",
                job_name=job_name,
                namespace=job_namespace,
                policy=self.settings.policy,
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

        logger.info(
            "job_scheduled",
            job_name=job_name,
            namespace=job_namespace,
            planned_start=window.start.isoformat(),
            policy=window.policy_used,
            mean_renewable_share=window.mean_renewable_share_pct,
        )

        record_job_held(job_namespace)

        return ScheduleDecision.hold_until(
            planned_start=window.start,
            policy_used=window.policy_used,
        )

    def should_release_now(
        self,
        parsed: ParsedAnnotations,
        current_time: datetime | None = None,
    ) -> bool:
        """Check if a held job should be released now.

        A job should be released if:
        1. The planned start time has been reached (within 60s tolerance)
        2. The deadline minus runtime minus safety margin has been reached

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
