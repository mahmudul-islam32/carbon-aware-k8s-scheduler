"""Backtest engine for evaluating scheduling policies.

Simulates job scheduling with historical grid data and compares
different scheduling policies.
"""

import csv
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from carbon_scheduler.planner.models import (
    Forecast,
    ForecastPoint,
    JobInfo,
    PolicyConfig,
)
from carbon_scheduler.planner.policies import PolicyName, get_policy
from carbon_scheduler.sim.historical import HistoricalData
from carbon_scheduler.sim.trace import JobTrace, TraceJob

logger = structlog.get_logger(__name__)

WATTS_PER_CPU_CORE = 10.0
WATTS_PER_GPU = 200.0


@dataclass
class JobResult:
    """Result of scheduling a single job with a policy."""

    job_name: str
    submit_time: datetime
    deadline: datetime
    runtime: timedelta
    scheduled_start: datetime
    scheduled_end: datetime
    delay: timedelta
    renewable_share_pct: float
    estimated_energy_kwh: float
    estimated_co2_grams: float
    deadline_met: bool
    is_fallback: bool
    policy: str
    cpu_cores: int
    gpu_units: int


@dataclass
class PolicyResult:
    """Aggregated results for a single policy."""

    policy_name: str
    job_results: list[JobResult] = field(default_factory=list)

    @property
    def total_jobs(self) -> int:
        return len(self.job_results)

    @property
    def deadline_misses(self) -> int:
        return sum(1 for j in self.job_results if not j.deadline_met)

    @property
    def deadline_miss_rate(self) -> float:
        if not self.job_results:
            return 0.0
        return self.deadline_misses / len(self.job_results)

    @property
    def total_co2_grams(self) -> float:
        return sum(j.estimated_co2_grams for j in self.job_results)

    @property
    def total_energy_kwh(self) -> float:
        return sum(j.estimated_energy_kwh for j in self.job_results)

    @property
    def mean_delay_seconds(self) -> float:
        if not self.job_results:
            return 0.0
        return sum(j.delay.total_seconds() for j in self.job_results) / len(self.job_results)

    @property
    def mean_renewable_share(self) -> float:
        if not self.job_results:
            return 0.0
        return sum(j.renewable_share_pct for j in self.job_results) / len(self.job_results)

    @property
    def fallback_count(self) -> int:
        return sum(1 for j in self.job_results if j.is_fallback)


@dataclass
class BacktestResult:
    """Complete backtest results comparing multiple policies."""

    policy_results: dict[str, PolicyResult] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def baseline_policy(self) -> str:
        return "immediate"

    def get_avoided_co2(self, policy_name: str) -> float:
        """Get CO2 avoided compared to immediate baseline."""
        if self.baseline_policy not in self.policy_results:
            return 0.0
        if policy_name not in self.policy_results:
            return 0.0

        baseline_co2 = self.policy_results[self.baseline_policy].total_co2_grams
        policy_co2 = self.policy_results[policy_name].total_co2_grams
        return baseline_co2 - policy_co2

    def get_avoided_co2_pct(self, policy_name: str) -> float:
        """Get percentage CO2 avoided compared to immediate baseline."""
        if self.baseline_policy not in self.policy_results:
            return 0.0

        baseline_co2 = self.policy_results[self.baseline_policy].total_co2_grams
        if baseline_co2 <= 0:
            return 0.0

        avoided = self.get_avoided_co2(policy_name)
        return (avoided / baseline_co2) * 100

    def to_csv_rows(self) -> list[dict[str, Any]]:
        """Convert all job results to CSV rows."""
        rows: list[dict[str, Any]] = []
        for policy_name, policy_result in self.policy_results.items():
            for job in policy_result.job_results:
                rows.append({
                    "policy": policy_name,
                    "job_name": job.job_name,
                    "submit_time": job.submit_time.isoformat(),
                    "deadline": job.deadline.isoformat(),
                    "runtime_seconds": job.runtime.total_seconds(),
                    "scheduled_start": job.scheduled_start.isoformat(),
                    "scheduled_end": job.scheduled_end.isoformat(),
                    "delay_seconds": job.delay.total_seconds(),
                    "renewable_share_pct": round(job.renewable_share_pct, 2),
                    "estimated_energy_kwh": round(job.estimated_energy_kwh, 4),
                    "estimated_co2_grams": round(job.estimated_co2_grams, 2),
                    "deadline_met": job.deadline_met,
                    "is_fallback": job.is_fallback,
                    "cpu_cores": job.cpu_cores,
                    "gpu_units": job.gpu_units,
                })
        return rows

    def save_csv(self, path: Path | str) -> None:
        """Save results to CSV file."""
        path = Path(path)
        rows = self.to_csv_rows()

        if not rows:
            return

        fieldnames = list(rows[0].keys())
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        logger.info("saved_backtest_csv", path=str(path), rows=len(rows))

    @classmethod
    def load_csv(cls, path: Path | str) -> "BacktestResult":
        """Load results from CSV file."""
        path = Path(path)
        result = cls()

        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                policy = row["policy"]
                if policy not in result.policy_results:
                    result.policy_results[policy] = PolicyResult(policy_name=policy)

                job_result = JobResult(
                    job_name=row["job_name"],
                    submit_time=datetime.fromisoformat(row["submit_time"]),
                    deadline=datetime.fromisoformat(row["deadline"]),
                    runtime=timedelta(seconds=float(row["runtime_seconds"])),
                    scheduled_start=datetime.fromisoformat(row["scheduled_start"]),
                    scheduled_end=datetime.fromisoformat(row["scheduled_end"]),
                    delay=timedelta(seconds=float(row["delay_seconds"])),
                    renewable_share_pct=float(row["renewable_share_pct"]),
                    estimated_energy_kwh=float(row["estimated_energy_kwh"]),
                    estimated_co2_grams=float(row["estimated_co2_grams"]),
                    deadline_met=row["deadline_met"].lower() == "true",
                    is_fallback=row["is_fallback"].lower() == "true",
                    policy=policy,
                    cpu_cores=int(row["cpu_cores"]),
                    gpu_units=int(row["gpu_units"]),
                )
                result.policy_results[policy].job_results.append(job_result)

        return result


class Backtest:
    """Backtest engine for comparing scheduling policies."""

    def __init__(
        self,
        historical_data: HistoricalData,
        config: PolicyConfig | None = None,
        carbon_intensity_gco2_kwh: float = 400.0,
    ) -> None:
        """Initialize the backtest engine.

        Args:
            historical_data: Historical grid data for simulation.
            config: Policy configuration.
            carbon_intensity_gco2_kwh: Base carbon intensity for calculations.
        """
        self.historical_data = historical_data
        self.config = config or PolicyConfig()
        self.base_carbon_intensity = carbon_intensity_gco2_kwh

    def _trace_job_to_job_info(self, trace_job: TraceJob) -> JobInfo:
        """Convert a TraceJob to JobInfo for policy evaluation."""
        return JobInfo(
            name=trace_job.name or "unnamed",
            submit_time=trace_job.submit_time,
            deadline=trace_job.deadline,
            estimated_runtime=trace_job.runtime,
        )

    def _create_forecast_from_historical(
        self,
        current_time: datetime,
        horizon: timedelta = timedelta(hours=24),
    ) -> Forecast:
        """Create a Forecast from historical data for policy evaluation.

        In a real system, policies use actual forecasts. For backtesting,
        we use historical data as if it were a perfect forecast.
        """
        end_time = current_time + horizon
        points: list[ForecastPoint] = []

        for point in self.historical_data.points:
            if current_time <= point.timestamp <= end_time:
                points.append(
                    ForecastPoint(
                        timestamp=point.timestamp,
                        renewable_share_pct=point.renewable_share_pct,
                    )
                )

        return Forecast(points=points, degraded=False)

    def _estimate_energy_kwh(
        self,
        cpu_cores: int,
        gpu_units: int,
        runtime: timedelta,
    ) -> float:
        """Estimate energy consumption for a job."""
        runtime_hours = runtime.total_seconds() / 3600.0
        total_watts = (cpu_cores * WATTS_PER_CPU_CORE) + (gpu_units * WATTS_PER_GPU)
        return total_watts * runtime_hours / 1000.0

    def _estimate_co2(
        self,
        energy_kwh: float,
        renewable_share_pct: float,
    ) -> float:
        """Estimate CO2 emissions based on renewable share.

        Higher renewable share = lower carbon intensity.
        Formula: CO2 = energy * intensity * (1 - renewable_share/100)
        """
        fossil_fraction = 1.0 - (renewable_share_pct / 100.0)
        effective_intensity = self.base_carbon_intensity * fossil_fraction
        return energy_kwh * effective_intensity

    def run_policy(
        self,
        trace: JobTrace,
        policy_name: PolicyName,
    ) -> PolicyResult:
        """Run a single policy against the job trace.

        Args:
            trace: The job trace to simulate.
            policy_name: Name of the policy to run.

        Returns:
            PolicyResult with results for all jobs.
        """
        policy = get_policy(policy_name, config=self.config)
        result = PolicyResult(policy_name=policy_name)

        for trace_job in trace.jobs:
            job_info = self._trace_job_to_job_info(trace_job)
            forecast = self._create_forecast_from_historical(
                trace_job.submit_time,
                horizon=trace_job.deadline - trace_job.submit_time + timedelta(hours=1),
            )

            window = policy.select(job_info, forecast, trace_job.submit_time)

            renewable_share = self.historical_data.get_average_renewable_share(
                window.start,
                window.end,
            )

            energy_kwh = self._estimate_energy_kwh(
                trace_job.cpu_cores,
                trace_job.gpu_units,
                trace_job.runtime,
            )

            co2_grams = self._estimate_co2(energy_kwh, renewable_share)

            delay = window.start - trace_job.submit_time

            deadline_met = window.end <= trace_job.deadline

            job_result = JobResult(
                job_name=trace_job.name,
                submit_time=trace_job.submit_time,
                deadline=trace_job.deadline,
                runtime=trace_job.runtime,
                scheduled_start=window.start,
                scheduled_end=window.end,
                delay=delay,
                renewable_share_pct=renewable_share,
                estimated_energy_kwh=energy_kwh,
                estimated_co2_grams=co2_grams,
                deadline_met=deadline_met,
                is_fallback=window.is_fallback,
                policy=policy_name,
                cpu_cores=trace_job.cpu_cores,
                gpu_units=trace_job.gpu_units,
            )

            result.job_results.append(job_result)

        logger.info(
            "policy_backtest_complete",
            policy=policy_name,
            jobs=result.total_jobs,
            deadline_misses=result.deadline_misses,
            total_co2_kg=round(result.total_co2_grams / 1000, 2),
        )

        return result

    def run(
        self,
        trace: JobTrace,
        policies: list[PolicyName],
    ) -> BacktestResult:
        """Run backtest comparing multiple policies.

        Args:
            trace: The job trace to simulate.
            policies: List of policy names to compare.

        Returns:
            BacktestResult with results for all policies.
        """
        result = BacktestResult()
        result.metadata = {
            "trace_jobs": len(trace.jobs),
            "trace_start": trace.start_time.isoformat() if trace.start_time else None,
            "trace_end": trace.end_time.isoformat() if trace.end_time else None,
            "policies": list(policies),
            "config": {
                "renewable_threshold_pct": self.config.renewable_threshold_pct,
                "safety_margin_seconds": self.config.safety_margin.total_seconds(),
            },
            "run_at": datetime.now().isoformat(),
        }

        for policy_name in policies:
            policy_result = self.run_policy(trace, policy_name)
            result.policy_results[policy_name] = policy_result

        logger.info(
            "backtest_complete",
            policies=len(policies),
            total_jobs=len(trace.jobs),
        )

        return result


__all__ = [
    "Backtest",
    "BacktestResult",
    "JobResult",
    "PolicyResult",
]
