"""Pydantic models for job and window data."""

from datetime import datetime, timedelta
from typing import Annotated

from pydantic import BaseModel, Field, field_validator


class ForecastPoint(BaseModel):
    """A single forecast data point."""

    timestamp: datetime
    renewable_share_pct: Annotated[float, Field(ge=0, le=100)]

    def __lt__(self, other: "ForecastPoint") -> bool:
        """Support sorting by timestamp."""
        return self.timestamp < other.timestamp


class Forecast(BaseModel):
    """Grid renewable share forecast data."""

    points: list[ForecastPoint] = Field(default_factory=list)
    degraded: bool = Field(
        default=False,
        description="True if data is from a substitute/fallback source",
    )

    @field_validator("points", mode="after")
    @classmethod
    def sort_points(cls, v: list[ForecastPoint]) -> list[ForecastPoint]:
        """Ensure points are sorted by timestamp."""
        return sorted(v)

    def get_points_in_range(
        self,
        start: datetime,
        end: datetime,
    ) -> list[ForecastPoint]:
        """Get forecast points within a time range (inclusive)."""
        return [p for p in self.points if start <= p.timestamp <= end]

    def mean_renewable_share(self, start: datetime, end: datetime) -> float | None:
        """Calculate mean renewable share for a time range.

        Returns None if no forecast points exist in the range.
        """
        points = self.get_points_in_range(start, end)
        if not points:
            return None
        return sum(p.renewable_share_pct for p in points) / len(points)


class JobInfo(BaseModel):
    """Information about a batch job for scheduling decisions."""

    name: str = Field(description="Job name/identifier")
    namespace: str = Field(default="default", description="Kubernetes namespace")
    submit_time: datetime = Field(description="When the job was submitted")
    deadline: datetime = Field(description="Latest time the job must complete by")
    estimated_runtime: timedelta = Field(description="Estimated job runtime")

    @field_validator("estimated_runtime", mode="before")
    @classmethod
    def parse_runtime(cls, v: timedelta | int | float | str) -> timedelta:
        """Parse runtime from various formats."""
        if isinstance(v, timedelta):
            return v
        if isinstance(v, int | float):
            return timedelta(seconds=v)
        if isinstance(v, str):
            return cls._parse_duration_string(v)
        raise ValueError(f"Cannot parse runtime from {type(v)}")

    @staticmethod
    def _parse_duration_string(s: str) -> timedelta:
        """Parse duration string like '1h30m' or '90m' or '3600s'."""
        s = s.strip().lower()
        total_seconds = 0.0

        import re

        pattern = r"(\d+(?:\.\d+)?)(d|h|m|s)"
        matches = re.findall(pattern, s)

        if not matches:
            if s.replace(".", "").isdigit():
                return timedelta(seconds=float(s))
            raise ValueError(f"Cannot parse duration: {s}")

        multipliers = {"d": 86400, "h": 3600, "m": 60, "s": 1}
        for value, unit in matches:
            total_seconds += float(value) * multipliers[unit]

        return timedelta(seconds=total_seconds)

    @property
    def estimated_runtime_seconds(self) -> float:
        """Get estimated runtime in seconds."""
        return self.estimated_runtime.total_seconds()

    def latest_feasible_start(self, safety_margin: timedelta) -> datetime:
        """Calculate the latest time the job can start and still meet deadline.

        Args:
            safety_margin: Safety buffer before the deadline.

        Returns:
            The latest possible start time.
        """
        return self.deadline - self.estimated_runtime - safety_margin


class ScheduleWindow(BaseModel):
    """A time window for scheduling a job."""

    start: datetime = Field(description="Window start time")
    end: datetime = Field(description="Window end time (job must complete by)")
    mean_renewable_share_pct: float | None = Field(
        default=None,
        description="Mean renewable share during this window",
    )
    policy_used: str = Field(description="Name of the policy that selected this window")
    is_fallback: bool = Field(
        default=False,
        description="True if this was selected due to fallback logic",
    )
    fallback_reason: str | None = Field(
        default=None,
        description="Reason for fallback if is_fallback is True",
    )

    @property
    def duration(self) -> timedelta:
        """Get the window duration."""
        return self.end - self.start

    def ends_before_or_at(self, deadline: datetime) -> bool:
        """Check if the window end is at or before the deadline."""
        return self.end <= deadline


class PolicyConfig(BaseModel):
    """Configuration for a scheduling policy."""

    renewable_threshold_pct: int = Field(
        default=60,
        ge=0,
        le=100,
        description="Threshold for threshold policy",
    )
    safety_margin: timedelta = Field(
        default=timedelta(minutes=5),
        description="Safety margin before deadline",
    )

    @field_validator("safety_margin", mode="before")
    @classmethod
    def parse_safety_margin(cls, v: timedelta | int | float) -> timedelta:
        """Parse safety margin from various formats."""
        if isinstance(v, timedelta):
            return v
        if isinstance(v, int | float):
            return timedelta(minutes=v)
        raise ValueError(f"Cannot parse safety margin from {type(v)}")
