"""Job trace generation and loading for backtesting.

Supports both synthetic trace generation (with configurable parameters)
and loading recorded traces from JSON files.
"""

import json
import random
import re
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, Field, field_validator

logger = structlog.get_logger(__name__)


class TraceJob(BaseModel):
    """A single job in a trace for backtesting."""

    submit_time: datetime = Field(description="When the job was submitted")
    deadline: datetime = Field(description="Job deadline (must complete by)")
    runtime: timedelta = Field(description="Estimated runtime of the job")
    cpu_cores: int = Field(default=4, ge=1, description="CPU cores requested")
    gpu_units: int = Field(default=0, ge=0, description="GPU units requested")
    name: str = Field(default="", description="Optional job name")

    @field_validator("runtime", mode="before")
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
    def slack(self) -> timedelta:
        """Get the slack time (deadline - submit_time - runtime)."""
        return self.deadline - self.submit_time - self.runtime

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "submit_time": self.submit_time.isoformat(),
            "deadline": self.deadline.isoformat(),
            "runtime": f"{int(self.runtime.total_seconds())}s",
            "cpu_cores": self.cpu_cores,
            "gpu_units": self.gpu_units,
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TraceJob":
        """Create from dictionary."""
        return cls(
            submit_time=datetime.fromisoformat(data["submit_time"]),
            deadline=datetime.fromisoformat(data["deadline"]),
            runtime=data["runtime"],
            cpu_cores=data.get("cpu_cores", 4),
            gpu_units=data.get("gpu_units", 0),
            name=data.get("name", ""),
        )


class JobTrace(BaseModel):
    """A collection of jobs for backtesting."""

    jobs: list[TraceJob] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.jobs)

    def __iter__(self) -> Iterator[TraceJob]:  # type: ignore[override]
        return iter(self.jobs)

    def __getitem__(self, idx: int) -> TraceJob:
        return self.jobs[idx]

    @property
    def start_time(self) -> datetime | None:
        """Get earliest submit time in the trace."""
        if not self.jobs:
            return None
        return min(j.submit_time for j in self.jobs)

    @property
    def end_time(self) -> datetime | None:
        """Get latest deadline in the trace."""
        if not self.jobs:
            return None
        return max(j.deadline for j in self.jobs)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "jobs": [j.to_dict() for j in self.jobs],
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobTrace":
        """Create from dictionary."""
        jobs_data = data.get("jobs", data) if isinstance(data, dict) else data
        if isinstance(jobs_data, list):
            return cls(
                jobs=[TraceJob.from_dict(j) for j in jobs_data],
                metadata=data.get("metadata", {}) if isinstance(data, dict) else {},
            )
        return cls(
            jobs=[TraceJob.from_dict(j) for j in jobs_data],
            metadata=data.get("metadata", {}),
        )

    def save(self, path: Path | str) -> None:
        """Save trace to JSON file."""
        path = Path(path)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
        logger.info("saved_trace", path=str(path), jobs=len(self.jobs))

    @classmethod
    def load(cls, path: Path | str) -> "JobTrace":
        """Load trace from JSON file."""
        path = Path(path)
        with open(path) as f:
            data = json.load(f)

        if isinstance(data, list):
            trace = cls(jobs=[TraceJob.from_dict(j) for j in data])
        else:
            trace = cls.from_dict(data)

        logger.info("loaded_trace", path=str(path), jobs=len(trace.jobs))
        return trace


def parse_slack_range(slack_str: str) -> tuple[timedelta, timedelta]:
    """Parse a slack range string like '1h-6h' into (min, max) timedeltas.

    Args:
        slack_str: Slack range string (e.g., '1h-6h', '30m-2h').

    Returns:
        Tuple of (min_slack, max_slack) as timedeltas.

    Raises:
        ValueError: If the format is invalid.
    """
    parts = slack_str.split("-")
    if len(parts) != 2:
        raise ValueError(f"Invalid slack range format: {slack_str}. Expected 'min-max' (e.g., '1h-6h')")

    min_slack = TraceJob._parse_duration_string(parts[0])
    max_slack = TraceJob._parse_duration_string(parts[1])

    if min_slack > max_slack:
        min_slack, max_slack = max_slack, min_slack

    return min_slack, max_slack


class SyntheticTraceGenerator:
    """Generates synthetic job traces for backtesting."""

    def __init__(self, seed: int | None = None) -> None:
        """Initialize the generator.

        Args:
            seed: Random seed for reproducibility. If None, uses random seed.
        """
        self.seed = seed
        self._rng = random.Random(seed)

    def generate(
        self,
        num_jobs: int,
        start_time: datetime,
        end_time: datetime,
        slack_min: timedelta = timedelta(hours=1),
        slack_max: timedelta = timedelta(hours=6),
        runtime_min: timedelta = timedelta(minutes=30),
        runtime_max: timedelta = timedelta(hours=2),
        cpu_cores_min: int = 1,
        cpu_cores_max: int = 16,
        gpu_probability: float = 0.1,
        gpu_max: int = 4,
    ) -> JobTrace:
        """Generate a synthetic job trace.

        Jobs are distributed uniformly across the time range. Runtimes and
        slack times are drawn from uniform distributions.

        Args:
            num_jobs: Number of jobs to generate.
            start_time: Earliest possible submit time.
            end_time: Latest possible deadline.
            slack_min: Minimum slack time (deadline - submit - runtime).
            slack_max: Maximum slack time.
            runtime_min: Minimum job runtime.
            runtime_max: Maximum job runtime.
            cpu_cores_min: Minimum CPU cores per job.
            cpu_cores_max: Maximum CPU cores per job.
            gpu_probability: Probability a job requests GPUs.
            gpu_max: Maximum GPU units per job (if requesting GPUs).

        Returns:
            JobTrace containing the generated jobs.
        """
        if self.seed is not None:
            self._rng = random.Random(self.seed)

        jobs: list[TraceJob] = []
        time_range_seconds = (end_time - start_time).total_seconds()

        for i in range(num_jobs):
            runtime_seconds = self._rng.uniform(
                runtime_min.total_seconds(),
                runtime_max.total_seconds(),
            )
            runtime = timedelta(seconds=runtime_seconds)

            slack_seconds = self._rng.uniform(
                slack_min.total_seconds(),
                slack_max.total_seconds(),
            )
            slack = timedelta(seconds=slack_seconds)

            max_submit_offset = time_range_seconds - runtime_seconds - slack_seconds
            if max_submit_offset < 0:
                max_submit_offset = 0

            submit_offset = self._rng.uniform(0, max_submit_offset)
            submit_time = start_time + timedelta(seconds=submit_offset)

            deadline = submit_time + runtime + slack

            if deadline > end_time:
                deadline = end_time

            cpu_cores = self._rng.randint(cpu_cores_min, cpu_cores_max)

            gpu_units = 0
            if self._rng.random() < gpu_probability:
                gpu_units = self._rng.randint(1, gpu_max)

            jobs.append(
                TraceJob(
                    submit_time=submit_time,
                    deadline=deadline,
                    runtime=runtime,
                    cpu_cores=cpu_cores,
                    gpu_units=gpu_units,
                    name=f"job-{i:04d}",
                )
            )

        jobs.sort(key=lambda j: j.submit_time)

        metadata = {
            "generated": True,
            "seed": self.seed,
            "num_jobs": num_jobs,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "slack_range": f"{slack_min}-{slack_max}",
            "runtime_range": f"{runtime_min}-{runtime_max}",
            "cpu_range": f"{cpu_cores_min}-{cpu_cores_max}",
            "gpu_probability": gpu_probability,
            "created_at": datetime.now().isoformat(),
        }

        logger.info(
            "generated_trace",
            num_jobs=num_jobs,
            seed=self.seed,
            start=start_time.isoformat(),
            end=end_time.isoformat(),
        )

        return JobTrace(jobs=jobs, metadata=metadata)


__all__ = [
    "JobTrace",
    "SyntheticTraceGenerator",
    "TraceJob",
    "parse_slack_range",
]
