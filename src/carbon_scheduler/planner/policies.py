"""Window selection policies for the carbon-aware scheduler.

Policies are pure functions that determine when a job should be scheduled
based on forecast data, job requirements, and current time. They implement
the scheduling strategies described in F-04 of the feature specification.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Literal

from carbon_scheduler.planner.models import (
    Forecast,
    JobInfo,
    PolicyConfig,
    ScheduleWindow,
)
from carbon_scheduler.planner.window import (
    Window,
    find_balanced_window,
    find_greenest_window,
    find_immediate_window,
    find_threshold_window,
)

PolicyName = Literal["immediate", "threshold", "greenest-window", "balanced"]

#: Every policy name the planner understands, in display order.
POLICY_NAMES: tuple[PolicyName, ...] = ("immediate", "threshold", "greenest-window", "balanced")


class Policy(ABC):
    """Abstract base class for scheduling policies.

    Policies are pure functions of:
    - The forecast data (timestamps and renewable share percentages)
    - The job info (submit time, deadline, estimated runtime)
    - Current time

    The chosen window must NEVER end after the deadline minus safety margin.
    If no suitable window is found, policies fall back to immediate start.
    """

    name: PolicyName

    def __init__(self, config: PolicyConfig | None = None) -> None:
        """Initialize the policy.

        Args:
            config: Policy configuration. Uses defaults if not provided.
        """
        self.config = config or PolicyConfig()

    @abstractmethod
    def select(
        self,
        job: JobInfo,
        forecast: Forecast | None,
        current_time: datetime,
    ) -> ScheduleWindow:
        """Select a scheduling window for the job.

        This is a pure function - given the same inputs, it always
        returns the same output.

        Args:
            job: Job information including deadline and estimated runtime.
            forecast: Grid forecast data, or None if unavailable.
            current_time: Current time for scheduling decisions.

        Returns:
            A ScheduleWindow that ends before the deadline (with safety margin).
            If no suitable window is found, returns an immediate start window
            with is_fallback=True.
        """
        ...

    def _validate_deadline_guarantee(
        self,
        window: ScheduleWindow,
        job: JobInfo,
    ) -> bool:
        """Validate that the window respects the deadline guarantee.

        Args:
            window: The selected scheduling window.
            job: Job information.

        Returns:
            True if the window end is before or at deadline minus safety margin.
        """
        deadline_with_margin = job.deadline - self.config.safety_margin
        return window.end <= deadline_with_margin

    def _create_fallback_window(
        self,
        job: JobInfo,
        current_time: datetime,
        reason: str,
    ) -> ScheduleWindow:
        """Create a fallback immediate-start window.

        Args:
            job: Job information.
            current_time: Current time.
            reason: Reason for fallback.

        Returns:
            An immediate start window marked as fallback.
        """
        immediate = find_immediate_window(job, current_time)
        return immediate.to_schedule_window(
            policy_name=self.name,
            is_fallback=True,
            fallback_reason=reason,
        )


class ImmediatePolicy(Policy):
    """Baseline policy that starts jobs immediately.

    This policy always returns a window starting at current_time (or submit_time,
    whichever is later) with duration equal to the job's estimated runtime.
    It serves as a baseline for comparing other policies.
    """

    name: PolicyName = "immediate"

    def select(
        self,
        job: JobInfo,
        forecast: Forecast | None,
        current_time: datetime,
    ) -> ScheduleWindow:
        """Select an immediate start window.

        Args:
            job: Job information.
            forecast: Ignored for this policy.
            current_time: Current time.

        Returns:
            A window starting immediately.
        """
        window = find_immediate_window(job, current_time)

        if forecast and forecast.points:
            window.forecast_points = forecast.get_points_in_range(
                window.start,
                window.end,
            )

        return window.to_schedule_window(policy_name=self.name)


class ThresholdPolicy(Policy):
    """Policy that waits for renewable share to meet a threshold.

    This policy finds the first forecast point where the renewable share
    percentage meets or exceeds the configured threshold. If no such point
    exists before the latest feasible start time, it falls back to the
    latest feasible start (deadline guarantee) or immediate start if
    that has also passed.
    """

    name: PolicyName = "threshold"

    def __init__(
        self,
        config: PolicyConfig | None = None,
        threshold_pct: float | None = None,
    ) -> None:
        """Initialize the threshold policy.

        Args:
            config: Policy configuration.
            threshold_pct: Optional override for the renewable threshold.
        """
        super().__init__(config)
        if threshold_pct is not None:
            # Copy so an override never leaks into a config shared with other policies
            self.config = self.config.model_copy(
                update={"renewable_threshold_pct": int(threshold_pct)}
            )

    def select(
        self,
        job: JobInfo,
        forecast: Forecast | None,
        current_time: datetime,
    ) -> ScheduleWindow:
        """Select a window where renewable share meets threshold.

        Args:
            job: Job information.
            forecast: Grid forecast data.
            current_time: Current time.

        Returns:
            A window starting at the first qualifying point, or a fallback window.
        """
        if forecast is None or not forecast.points:
            return self._create_fallback_window(
                job,
                current_time,
                "no_forecast_data",
            )

        if forecast.degraded:
            return self._create_fallback_window(
                job,
                current_time,
                "forecast_data_degraded",
            )

        latest_feasible_start = job.latest_feasible_start(self.config.safety_margin)

        if current_time > latest_feasible_start:
            return self._create_fallback_window(
                job,
                current_time,
                "current_time_past_latest_feasible_start",
            )

        window = find_threshold_window(
            job=job,
            forecast=forecast,
            current_time=current_time,
            threshold_pct=self.config.renewable_threshold_pct,
            config=self.config,
        )

        if window is None:
            return self._create_latest_feasible_or_fallback(
                job=job,
                forecast=forecast,
                current_time=current_time,
                reason="no_threshold_window_found",
            )

        return window.to_schedule_window(policy_name=self.name)

    def _create_latest_feasible_or_fallback(
        self,
        job: JobInfo,
        forecast: Forecast,
        current_time: datetime,
        reason: str,
    ) -> ScheduleWindow:
        """Create a window at the latest feasible start time.

        This ensures we meet the deadline even if no threshold window is found.

        Args:
            job: Job information.
            forecast: Forecast data.
            current_time: Current time.
            reason: Reason for this selection.

        Returns:
            A window at the latest feasible start, marked as fallback.
        """
        latest_feasible_start = job.latest_feasible_start(self.config.safety_margin)

        if current_time > latest_feasible_start:
            return self._create_fallback_window(job, current_time, reason)

        window_end = latest_feasible_start + job.estimated_runtime
        window_points = forecast.get_points_in_range(latest_feasible_start, window_end)

        window = Window(
            start=latest_feasible_start,
            end=window_end,
            forecast_points=window_points,
        )

        return window.to_schedule_window(
            policy_name=self.name,
            is_fallback=True,
            fallback_reason=reason,
        )


class GreenestWindowPolicy(Policy):
    """Policy that finds the window with highest mean renewable share.

    This policy searches all possible windows of the job's estimated runtime
    length between current_time and the latest feasible start time. It returns
    the window with the highest mean renewable share percentage.

    If no valid window can be found (e.g., deadline too soon, no forecast data),
    it falls back to the latest feasible start or immediate start.
    """

    name: PolicyName = "greenest-window"

    def select(
        self,
        job: JobInfo,
        forecast: Forecast | None,
        current_time: datetime,
    ) -> ScheduleWindow:
        """Select the greenest available window.

        Args:
            job: Job information.
            forecast: Grid forecast data.
            current_time: Current time.

        Returns:
            The window with highest mean renewable share, or a fallback window.
        """
        if forecast is None or not forecast.points:
            return self._create_fallback_window(
                job,
                current_time,
                "no_forecast_data",
            )

        if forecast.degraded:
            return self._create_fallback_window(
                job,
                current_time,
                "forecast_data_degraded",
            )

        latest_feasible_start = job.latest_feasible_start(self.config.safety_margin)

        if current_time > latest_feasible_start:
            return self._create_fallback_window(
                job,
                current_time,
                "current_time_past_latest_feasible_start",
            )

        window = find_greenest_window(
            job=job,
            forecast=forecast,
            current_time=current_time,
            config=self.config,
        )

        if window is None:
            return self._create_fallback_window(
                job,
                current_time,
                "no_greenest_window_found",
            )

        return window.to_schedule_window(policy_name=self.name)


class BalancedPolicy(Policy):
    """Policy that trades renewable share against waiting time.

    Scores each feasible window as mean share minus a per-hour delay penalty
    (``PolicyConfig.delay_penalty_pct_per_hour``). Useful when jobs have long
    deadlines but owners still prefer results sooner when the gain is small.
    """

    name: PolicyName = "balanced"

    def __init__(
        self,
        config: PolicyConfig | None = None,
        delay_penalty_pct_per_hour: float | None = None,
    ) -> None:
        super().__init__(config)
        if delay_penalty_pct_per_hour is not None:
            self.config = self.config.model_copy(
                update={"delay_penalty_pct_per_hour": float(delay_penalty_pct_per_hour)}
            )

    def select(
        self,
        job: JobInfo,
        forecast: Forecast | None,
        current_time: datetime,
    ) -> ScheduleWindow:
        """Select the best-scoring window, falling back to an immediate start."""
        if forecast is None or not forecast.points:
            return self._create_fallback_window(job, current_time, "no_forecast_data")
        if forecast.degraded:
            return self._create_fallback_window(job, current_time, "forecast_data_degraded")
        if current_time > job.latest_feasible_start(self.config.safety_margin):
            return self._create_fallback_window(
                job, current_time, "current_time_past_latest_feasible_start"
            )

        window = find_balanced_window(job, forecast, current_time, self.config)
        if window is None:
            return self._create_fallback_window(job, current_time, "no_balanced_window_found")
        return window.to_schedule_window(policy_name=self.name)


def get_policy(
    name: PolicyName,
    config: PolicyConfig | None = None,
    threshold_pct: float | None = None,
    delay_penalty_pct_per_hour: float | None = None,
) -> Policy:
    """Factory function to get a policy by name.

    Args:
        name: Policy name (see ``POLICY_NAMES``).
        config: Optional policy configuration.
        threshold_pct: Optional threshold override for threshold policy.
        delay_penalty_pct_per_hour: Optional penalty override for the balanced policy.

    Returns:
        The requested policy instance.

    Raises:
        ValueError: If the policy name is not recognized.
    """
    policies: dict[PolicyName, type[Policy]] = {
        "immediate": ImmediatePolicy,
        "threshold": ThresholdPolicy,
        "greenest-window": GreenestWindowPolicy,
        "balanced": BalancedPolicy,
    }

    if name not in policies:
        valid = ", ".join(policies.keys())
        raise ValueError(f"Unknown policy: {name}. Valid policies: {valid}")

    policy_class = policies[name]

    if name == "threshold" and threshold_pct is not None:
        return ThresholdPolicy(config=config, threshold_pct=threshold_pct)

    if name == "balanced":
        return BalancedPolicy(config=config, delay_penalty_pct_per_hour=delay_penalty_pct_per_hour)

    return policy_class(config=config)
