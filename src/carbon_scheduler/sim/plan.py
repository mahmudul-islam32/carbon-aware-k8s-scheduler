"""Dry-run planning: show which window the controller would pick for a job.

Used by ``carbon-sim plan`` so job owners can check a deadline and runtime
against the current forecast before submitting a Job.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from carbon_scheduler.carbon.live import (
    AvoidedEmissions,
    JobResources,
    estimate_avoided_emissions,
)
from carbon_scheduler.planner.models import (
    Forecast,
    ForecastPoint,
    JobInfo,
    PolicyConfig,
    ScheduleWindow,
)
from carbon_scheduler.planner.policies import PolicyName, get_policy

SPARK_CHARS = "▁▂▃▄▅▆▇█"


@dataclass
class PlanResult:
    """Outcome of a dry-run plan."""

    submitted: datetime
    window: ScheduleWindow
    latest_start: datetime
    avoided: AvoidedEmissions | None

    @property
    def delay(self) -> timedelta:
        """How long the job would be held."""
        return max(timedelta(0), self.window.start - self.submitted)


def forecast_from_energy_charts(data: dict[str, Any]) -> Forecast:
    """Build a planner Forecast from a raw ``/ren_share_forecast`` response."""
    points = [
        ForecastPoint(
            timestamp=datetime.fromtimestamp(ts, tz=UTC),
            renewable_share_pct=share,
        )
        for ts, share in zip(data.get("unix_seconds", []), data.get("ren_share", []), strict=False)
        if share is not None
    ]
    return Forecast(points=points, degraded=bool(data.get("substitute", False)))


def load_forecast_file(path: Path) -> Forecast:
    """Load a saved ``/ren_share_forecast`` JSON response from disk."""
    with path.open() as f:
        data: dict[str, Any] = json.load(f)
    return forecast_from_energy_charts(data)


def plan_job(
    forecast: Forecast,
    now: datetime,
    runtime: timedelta,
    deadline: datetime,
    policy: PolicyName = "greenest-window",
    threshold_pct: int = 60,
    safety_margin: timedelta = timedelta(minutes=5),
    resources: JobResources | None = None,
    fossil_intensity_gco2_kwh: float = 400.0,
) -> PlanResult:
    """Run a policy against a forecast exactly as the controller would."""
    config = PolicyConfig(renewable_threshold_pct=threshold_pct, safety_margin=safety_margin)
    job = JobInfo(
        name="dry-run",
        submit_time=now,
        deadline=deadline,
        estimated_runtime=runtime,
    )
    window = get_policy(policy, config=config, threshold_pct=threshold_pct).select(
        job, forecast, now
    )
    avoided = estimate_avoided_emissions(
        forecast=forecast,
        submit_time=now,
        planned_start=window.start,
        runtime=runtime,
        resources=resources or JobResources(cpu_cores=1.0, gpu_units=0.0),
        fossil_intensity_gco2_kwh=fossil_intensity_gco2_kwh,
    )
    return PlanResult(
        submitted=now,
        window=window,
        latest_start=job.latest_feasible_start(safety_margin),
        avoided=avoided,
    )


def sparkline(values: list[float], low: float = 0.0, high: float = 100.0) -> str:
    """Render values as a unicode sparkline scaled between ``low`` and ``high``."""
    if not values:
        return ""
    span = max(high - low, 1e-9)
    last = len(SPARK_CHARS) - 1
    return "".join(
        SPARK_CHARS[min(last, max(0, round((v - low) / span * last)))] for v in values
    )
