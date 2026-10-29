"""Live CO2 accounting for jobs handled by the controller.

The controller only has the renewable-share forecast at planning time, so live
accounting uses the same renewable-share proxy as the backtest engine:

    intensity (gCO2/kWh) = fossil_intensity * (1 - renewable_share / 100)

Job energy is estimated from the Job's pod template resource requests:

    energy (kWh) = (cpu_cores * W/core + gpus * W/GPU) * parallelism * runtime_hours

Avoided CO2 = emissions(window starting at submit) - emissions(planned window).
All outputs are ESTIMATES.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from carbon_scheduler.planner.models import Forecast

GPU_RESOURCE_KEYS = ("nvidia.com/gpu", "amd.com/gpu", "gpu.intel.com/i915")

_CPU_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)(m?)$")


@dataclass(frozen=True)
class JobResources:
    """Compute resources requested by a Job (summed over containers and parallelism)."""

    cpu_cores: float
    gpu_units: float


@dataclass(frozen=True)
class AvoidedEmissions:
    """Estimated emissions of the planned window vs an immediate start."""

    energy_kwh: float
    baseline_share_pct: float
    planned_share_pct: float
    baseline_gco2: float
    planned_gco2: float

    @property
    def avoided_gco2(self) -> float:
        """Estimated CO2 avoided in grams (negative means more was emitted)."""
        return self.baseline_gco2 - self.planned_gco2


def parse_cpu_quantity(value: Any) -> float:
    """Parse a Kubernetes CPU quantity ('500m', '2', '1.5') into cores.

    Returns 0.0 for values that cannot be parsed.
    """
    if value is None:
        return 0.0
    match = _CPU_PATTERN.match(str(value).strip())
    if not match:
        return 0.0
    number = float(match.group(1))
    return number / 1000.0 if match.group(2) == "m" else number


def extract_job_resources(job_spec: dict[str, Any] | None) -> JobResources:
    """Sum CPU and GPU requests over all containers of a Job's pod template.

    Falls back to limits when a request is missing, and to one CPU core when the
    Job requests nothing at all. The result is multiplied by ``spec.parallelism``.
    """
    if not job_spec:
        return JobResources(cpu_cores=1.0, gpu_units=0.0)

    pod_spec = job_spec.get("template", {}).get("spec", {}) or {}
    containers = pod_spec.get("containers", []) or []

    cpu = 0.0
    gpu = 0.0
    for container in containers:
        resources = container.get("resources", {}) or {}
        requests = resources.get("requests", {}) or {}
        limits = resources.get("limits", {}) or {}

        cpu += parse_cpu_quantity(requests.get("cpu", limits.get("cpu")))
        for key in GPU_RESOURCE_KEYS:
            raw = requests.get(key, limits.get(key))
            if raw is not None:
                try:
                    gpu += float(raw)
                except (TypeError, ValueError):
                    continue

    if cpu == 0.0 and gpu == 0.0:
        cpu = 1.0

    parallelism = job_spec.get("parallelism") or 1
    try:
        factor = max(1, int(parallelism))
    except (TypeError, ValueError):
        factor = 1

    return JobResources(cpu_cores=cpu * factor, gpu_units=gpu * factor)


def estimate_energy_kwh(
    resources: JobResources,
    runtime: timedelta,
    watts_per_cpu_core: float,
    watts_per_gpu: float,
) -> float:
    """Estimate energy use in kWh for the requested resources and runtime."""
    watts = resources.cpu_cores * watts_per_cpu_core + resources.gpu_units * watts_per_gpu
    return watts * (runtime.total_seconds() / 3600.0) / 1000.0


def share_to_intensity(renewable_share_pct: float, fossil_intensity_gco2_kwh: float) -> float:
    """Convert a renewable share into an estimated carbon intensity (gCO2/kWh)."""
    share = min(100.0, max(0.0, renewable_share_pct))
    return fossil_intensity_gco2_kwh * (1.0 - share / 100.0)


def mean_share_for_window(
    forecast: Forecast,
    start: datetime,
    runtime: timedelta,
) -> float | None:
    """Mean forecast renewable share over [start, start + runtime].

    When no point falls inside a short window, the closest earlier point is used.
    """
    end = start + runtime
    share = forecast.mean_renewable_share(start, end)
    if share is not None:
        return share
    earlier = [p for p in forecast.points if p.timestamp <= start]
    return earlier[-1].renewable_share_pct if earlier else None


def estimate_avoided_emissions(
    forecast: Forecast,
    submit_time: datetime,
    planned_start: datetime,
    runtime: timedelta,
    resources: JobResources,
    fossil_intensity_gco2_kwh: float = 400.0,
    watts_per_cpu_core: float = 10.0,
    watts_per_gpu: float = 200.0,
) -> AvoidedEmissions | None:
    """Estimate CO2 avoided by starting at ``planned_start`` instead of ``submit_time``.

    Returns None when the forecast does not cover both windows.
    """
    baseline_share = mean_share_for_window(forecast, submit_time, runtime)
    planned_share = mean_share_for_window(forecast, planned_start, runtime)
    if baseline_share is None or planned_share is None:
        return None

    energy = estimate_energy_kwh(resources, runtime, watts_per_cpu_core, watts_per_gpu)

    return AvoidedEmissions(
        energy_kwh=energy,
        baseline_share_pct=baseline_share,
        planned_share_pct=planned_share,
        baseline_gco2=energy * share_to_intensity(baseline_share, fossil_intensity_gco2_kwh),
        planned_gco2=energy * share_to_intensity(planned_share, fossil_intensity_gco2_kwh),
    )
