"""Window selection logic for scheduling jobs."""

from datetime import datetime, timedelta

from carbon_scheduler.planner.models import (
    Forecast,
    ForecastPoint,
    JobInfo,
    PolicyConfig,
    ScheduleWindow,
)


class Window:
    """Represents a contiguous time window with associated forecast data.

    This is used internally by the window selection algorithms to track
    candidate scheduling windows and their renewable share statistics.
    """

    def __init__(
        self,
        start: datetime,
        end: datetime,
        forecast_points: list[ForecastPoint] | None = None,
    ) -> None:
        """Initialize a window.

        Args:
            start: Window start time.
            end: Window end time.
            forecast_points: Optional list of forecast points within this window.
        """
        self.start = start
        self.end = end
        self.forecast_points = forecast_points or []

    @property
    def duration(self) -> timedelta:
        """Get the window duration."""
        return self.end - self.start

    @property
    def mean_renewable_share(self) -> float | None:
        """Calculate mean renewable share for this window.

        Returns:
            Mean renewable share percentage, or None if no forecast points.
        """
        if not self.forecast_points:
            return None
        return sum(p.renewable_share_pct for p in self.forecast_points) / len(
            self.forecast_points
        )

    def contains_timestamp(self, ts: datetime) -> bool:
        """Check if a timestamp falls within this window."""
        return self.start <= ts <= self.end

    def ends_before_or_at(self, deadline: datetime) -> bool:
        """Check if the window end is at or before the deadline."""
        return self.end <= deadline

    def to_schedule_window(
        self,
        policy_name: str,
        is_fallback: bool = False,
        fallback_reason: str | None = None,
    ) -> ScheduleWindow:
        """Convert to a ScheduleWindow model."""
        return ScheduleWindow(
            start=self.start,
            end=self.end,
            mean_renewable_share_pct=self.mean_renewable_share,
            policy_used=policy_name,
            is_fallback=is_fallback,
            fallback_reason=fallback_reason,
        )


def find_immediate_window(
    job: JobInfo,
    current_time: datetime,
) -> Window:
    """Create an immediate start window.

    Args:
        job: Job information.
        current_time: Current time.

    Returns:
        A window starting now and ending at now + estimated_runtime.
    """
    start = max(current_time, job.submit_time)
    end = start + job.estimated_runtime
    return Window(start=start, end=end)


def find_threshold_window(
    job: JobInfo,
    forecast: Forecast,
    current_time: datetime,
    threshold_pct: float,
    config: PolicyConfig,
) -> Window | None:
    """Find the first window where renewable share >= threshold.

    Searches forecast points starting from current_time to find the first
    point where the renewable share meets or exceeds the threshold.
    The window must end before the deadline minus safety margin.

    Args:
        job: Job information.
        forecast: Forecast data.
        current_time: Current time.
        threshold_pct: Minimum renewable share percentage.
        config: Policy configuration.

    Returns:
        A window starting at the first qualifying point, or None if none found.
    """
    latest_feasible_start = job.latest_feasible_start(config.safety_margin)

    if current_time > latest_feasible_start:
        return None

    for point in forecast.points:
        if point.timestamp < current_time:
            continue

        if point.timestamp > latest_feasible_start:
            break

        if point.renewable_share_pct >= threshold_pct:
            window_end = point.timestamp + job.estimated_runtime
            deadline_with_margin = job.deadline - config.safety_margin

            if window_end <= deadline_with_margin:
                window_points = forecast.get_points_in_range(point.timestamp, window_end)
                return Window(
                    start=point.timestamp,
                    end=window_end,
                    forecast_points=window_points,
                )

    return None


def find_greenest_window(
    job: JobInfo,
    forecast: Forecast,
    current_time: datetime,
    config: PolicyConfig,
) -> Window | None:
    """Find the window with highest mean renewable share.

    Searches all possible windows of length estimated_runtime between
    current_time and the latest feasible start time. Returns the window
    with the highest mean renewable share.

    Args:
        job: Job information.
        forecast: Forecast data.
        current_time: Current time.
        config: Policy configuration.

    Returns:
        The greenest window, or None if no valid window exists.
    """
    latest_feasible_start = job.latest_feasible_start(config.safety_margin)

    if current_time > latest_feasible_start:
        return None

    if not forecast.points:
        return None

    start_time = max(current_time, job.submit_time)

    candidate_starts = [start_time]

    for point in forecast.points:
        if start_time <= point.timestamp <= latest_feasible_start:
            candidate_starts.append(point.timestamp)

    candidate_starts = sorted(set(candidate_starts))

    best_window: Window | None = None
    best_mean_share: float = -1.0

    for start in candidate_starts:
        window_end = start + job.estimated_runtime
        deadline_with_margin = job.deadline - config.safety_margin

        if window_end > deadline_with_margin:
            continue

        window_points = forecast.get_points_in_range(start, window_end)

        if not window_points:
            continue

        mean_share = sum(p.renewable_share_pct for p in window_points) / len(window_points)

        if mean_share > best_mean_share:
            best_mean_share = mean_share
            best_window = Window(
                start=start,
                end=window_end,
                forecast_points=window_points,
            )

    return best_window


def select_window(
    job: JobInfo,
    forecast: Forecast | None,
    current_time: datetime,
    policy_name: str,
    config: PolicyConfig,
) -> ScheduleWindow:
    """Select a scheduling window using the specified policy.

    This is the main entry point for window selection. It applies the named
    policy and handles fallback logic when the policy cannot find a valid
    window or when forecast data is unavailable.

    Args:
        job: Job information.
        forecast: Forecast data, or None if unavailable.
        current_time: Current time.
        policy_name: Name of the policy to use ('immediate', 'threshold', 'greenest-window').
        config: Policy configuration.

    Returns:
        A ScheduleWindow representing the selected scheduling window.
        The window is guaranteed to end before the deadline (with safety margin).
    """
    latest_feasible_start = job.latest_feasible_start(config.safety_margin)

    if current_time > latest_feasible_start:
        immediate = find_immediate_window(job, current_time)
        return immediate.to_schedule_window(
            policy_name=policy_name,
            is_fallback=True,
            fallback_reason="current_time_past_latest_feasible_start",
        )

    if policy_name == "immediate":
        immediate = find_immediate_window(job, current_time)
        return immediate.to_schedule_window(policy_name="immediate")

    if forecast is None or not forecast.points:
        immediate = find_immediate_window(job, current_time)
        return immediate.to_schedule_window(
            policy_name=policy_name,
            is_fallback=True,
            fallback_reason="no_forecast_data",
        )

    if forecast.degraded:
        immediate = find_immediate_window(job, current_time)
        return immediate.to_schedule_window(
            policy_name=policy_name,
            is_fallback=True,
            fallback_reason="forecast_data_degraded",
        )

    window: Window | None = None

    if policy_name == "threshold":
        window = find_threshold_window(
            job=job,
            forecast=forecast,
            current_time=current_time,
            threshold_pct=config.renewable_threshold_pct,
            config=config,
        )
    elif policy_name == "greenest-window":
        window = find_greenest_window(
            job=job,
            forecast=forecast,
            current_time=current_time,
            config=config,
        )
    else:
        immediate = find_immediate_window(job, current_time)
        return immediate.to_schedule_window(
            policy_name=policy_name,
            is_fallback=True,
            fallback_reason=f"unknown_policy_{policy_name}",
        )

    if window is None:
        immediate = find_immediate_window(job, current_time)
        return immediate.to_schedule_window(
            policy_name=policy_name,
            is_fallback=True,
            fallback_reason="no_suitable_window_found",
        )

    return window.to_schedule_window(policy_name=policy_name)
