"""Carbon accounting for job emissions and counterfactual comparison.

This module calculates:
- Job emissions at actual start time
- Counterfactual emissions (what would have been emitted if job started immediately)
- Avoided CO2 = counterfactual - actual
- Cumulative savings tracking

All outputs are clearly labeled as ESTIMATES.
"""

from datetime import datetime, timedelta

from carbon_scheduler.carbon.estimator import CarbonEstimator
from carbon_scheduler.carbon.models import (
    CarbonIntensityEstimate,
    CounterfactualComparison,
    CumulativeSavings,
    JobEmissionsEstimate,
    JobEnergyEstimate,
)


class JobEmissions:
    """Calculate emissions for a single job.

    This class handles energy and emissions estimation for a job
    at a given start time.
    """

    def __init__(
        self,
        estimator: CarbonEstimator,
        watts_per_cpu_core: float = 10.0,
        watts_per_gpu: float = 200.0,
    ) -> None:
        """Initialize job emissions calculator.

        Args:
            estimator: CarbonEstimator instance for intensity calculations.
            watts_per_cpu_core: Power draw per CPU core in watts.
            watts_per_gpu: Power draw per GPU in watts.
        """
        self._estimator = estimator
        self._watts_per_cpu_core = watts_per_cpu_core
        self._watts_per_gpu = watts_per_gpu

    def estimate_energy(
        self,
        job_name: str,
        job_namespace: str,
        cpu_cores: float,
        gpu_units: float,
        runtime_seconds: float,
    ) -> JobEnergyEstimate:
        """Estimate energy consumption for a job.

        Args:
            job_name: Name of the Kubernetes job.
            job_namespace: Namespace of the job.
            cpu_cores: Requested CPU cores.
            gpu_units: Requested GPU units.
            runtime_seconds: Job runtime in seconds.

        Returns:
            JobEnergyEstimate with energy consumption in kWh (ESTIMATE).
        """
        energy_kwh = self._estimator.estimate_job_energy_kwh(
            cpu_cores=cpu_cores,
            gpu_units=gpu_units,
            runtime_seconds=runtime_seconds,
            watts_per_cpu_core=self._watts_per_cpu_core,
            watts_per_gpu=self._watts_per_gpu,
        )

        return JobEnergyEstimate(
            job_name=job_name,
            job_namespace=job_namespace,
            cpu_cores=cpu_cores,
            gpu_units=gpu_units,
            runtime_seconds=runtime_seconds,
            estimated_energy_kwh=energy_kwh,
            is_estimate=True,
        )

    def estimate_emissions(
        self,
        job_name: str,
        job_namespace: str,
        cpu_cores: float,
        gpu_units: float,
        runtime_seconds: float,
        start_time: datetime,
        intensities: list[CarbonIntensityEstimate],
    ) -> JobEmissionsEstimate:
        """Estimate emissions for a job at a given start time.

        Args:
            job_name: Name of the Kubernetes job.
            job_namespace: Namespace of the job.
            cpu_cores: Requested CPU cores.
            gpu_units: Requested GPU units.
            runtime_seconds: Job runtime in seconds.
            start_time: When the job starts.
            intensities: List of carbon intensity estimates for interpolation.

        Returns:
            JobEmissionsEstimate with emissions in gCO2 (ESTIMATE).
        """
        energy = self.estimate_energy(
            job_name=job_name,
            job_namespace=job_namespace,
            cpu_cores=cpu_cores,
            gpu_units=gpu_units,
            runtime_seconds=runtime_seconds,
        )

        duration = timedelta(seconds=runtime_seconds)
        avg_intensity = self._estimator.estimate_average_intensity_for_window(
            intensities=intensities,
            start_time=start_time,
            duration=duration,
        )

        emissions_gco2 = self._estimator.estimate_emissions_gco2(
            energy_kwh=energy.estimated_energy_kwh,
            carbon_intensity_gco2_kwh=avg_intensity,
        )

        end_time = start_time + duration

        return JobEmissionsEstimate(
            job_name=job_name,
            job_namespace=job_namespace,
            start_time=start_time,
            end_time=end_time,
            estimated_energy_kwh=energy.estimated_energy_kwh,
            estimated_carbon_intensity_gco2_kwh=avg_intensity,
            estimated_emissions_gco2=emissions_gco2,
            is_estimate=True,
        )


class CarbonAccounting:
    """Track and compare job emissions with counterfactual analysis.

    This class calculates avoided CO2 by comparing actual emissions
    (at the scheduled start time) with counterfactual emissions
    (if the job had started immediately at submit time).
    """

    def __init__(
        self,
        estimator: CarbonEstimator,
        watts_per_cpu_core: float = 10.0,
        watts_per_gpu: float = 200.0,
    ) -> None:
        """Initialize carbon accounting.

        Args:
            estimator: CarbonEstimator instance.
            watts_per_cpu_core: Power draw per CPU core in watts.
            watts_per_gpu: Power draw per GPU in watts.
        """
        self._job_emissions = JobEmissions(
            estimator=estimator,
            watts_per_cpu_core=watts_per_cpu_core,
            watts_per_gpu=watts_per_gpu,
        )
        self._tracked_comparisons: list[CounterfactualComparison] = []

    def compare_counterfactual(
        self,
        job_name: str,
        job_namespace: str,
        cpu_cores: float,
        gpu_units: float,
        runtime_seconds: float,
        submit_time: datetime,
        actual_start_time: datetime,
        intensities: list[CarbonIntensityEstimate],
    ) -> CounterfactualComparison:
        """Compare actual emissions with counterfactual (immediate start).

        Avoided CO2 = counterfactual emissions - actual emissions
        Positive values indicate carbon savings from delayed start.

        Args:
            job_name: Name of the Kubernetes job.
            job_namespace: Namespace of the job.
            cpu_cores: Requested CPU cores.
            gpu_units: Requested GPU units.
            runtime_seconds: Job runtime in seconds.
            submit_time: When the job was submitted.
            actual_start_time: When the job actually started.
            intensities: List of carbon intensity estimates.

        Returns:
            CounterfactualComparison with avoided emissions (ESTIMATE).
        """
        actual_emissions = self._job_emissions.estimate_emissions(
            job_name=job_name,
            job_namespace=job_namespace,
            cpu_cores=cpu_cores,
            gpu_units=gpu_units,
            runtime_seconds=runtime_seconds,
            start_time=actual_start_time,
            intensities=intensities,
        )

        counterfactual_emissions = self._job_emissions.estimate_emissions(
            job_name=job_name,
            job_namespace=job_namespace,
            cpu_cores=cpu_cores,
            gpu_units=gpu_units,
            runtime_seconds=runtime_seconds,
            start_time=submit_time,
            intensities=intensities,
        )

        avoided = (
            counterfactual_emissions.estimated_emissions_gco2
            - actual_emissions.estimated_emissions_gco2
        )

        if counterfactual_emissions.estimated_emissions_gco2 > 0:
            savings_pct = (avoided / counterfactual_emissions.estimated_emissions_gco2) * 100
        else:
            savings_pct = 0.0

        return CounterfactualComparison(
            job_name=job_name,
            job_namespace=job_namespace,
            submit_time=submit_time,
            actual_start_time=actual_start_time,
            runtime_seconds=runtime_seconds,
            actual_emissions_gco2=actual_emissions.estimated_emissions_gco2,
            counterfactual_emissions_gco2=counterfactual_emissions.estimated_emissions_gco2,
            avoided_emissions_gco2=avoided,
            savings_percent=savings_pct,
            is_estimate=True,
        )

    def track_comparison(self, comparison: CounterfactualComparison) -> None:
        """Add a comparison to the cumulative tracking.

        Args:
            comparison: CounterfactualComparison to track.
        """
        self._tracked_comparisons.append(comparison)

    def compare_and_track(
        self,
        job_name: str,
        job_namespace: str,
        cpu_cores: float,
        gpu_units: float,
        runtime_seconds: float,
        submit_time: datetime,
        actual_start_time: datetime,
        intensities: list[CarbonIntensityEstimate],
    ) -> CounterfactualComparison:
        """Compare and track in one operation.

        Args:
            job_name: Name of the Kubernetes job.
            job_namespace: Namespace of the job.
            cpu_cores: Requested CPU cores.
            gpu_units: Requested GPU units.
            runtime_seconds: Job runtime in seconds.
            submit_time: When the job was submitted.
            actual_start_time: When the job actually started.
            intensities: List of carbon intensity estimates.

        Returns:
            CounterfactualComparison that has been tracked.
        """
        comparison = self.compare_counterfactual(
            job_name=job_name,
            job_namespace=job_namespace,
            cpu_cores=cpu_cores,
            gpu_units=gpu_units,
            runtime_seconds=runtime_seconds,
            submit_time=submit_time,
            actual_start_time=actual_start_time,
            intensities=intensities,
        )
        self.track_comparison(comparison)
        return comparison

    def get_cumulative_savings(self) -> CumulativeSavings:
        """Get cumulative savings across all tracked jobs.

        Returns:
            CumulativeSavings with totals and averages (ESTIMATES).
        """
        if not self._tracked_comparisons:
            return CumulativeSavings(
                total_jobs=0,
                total_avoided_gco2=0.0,
                total_avoided_kg_co2=0.0,
                total_actual_emissions_gco2=0.0,
                total_counterfactual_emissions_gco2=0.0,
                average_savings_percent=0.0,
                is_estimate=True,
            )

        total_avoided = sum(c.avoided_emissions_gco2 for c in self._tracked_comparisons)
        total_actual = sum(c.actual_emissions_gco2 for c in self._tracked_comparisons)
        total_counterfactual = sum(
            c.counterfactual_emissions_gco2 for c in self._tracked_comparisons
        )

        if total_counterfactual > 0:
            avg_savings = (total_avoided / total_counterfactual) * 100
        else:
            avg_savings = 0.0

        return CumulativeSavings(
            total_jobs=len(self._tracked_comparisons),
            total_avoided_gco2=total_avoided,
            total_avoided_kg_co2=total_avoided / 1000.0,
            total_actual_emissions_gco2=total_actual,
            total_counterfactual_emissions_gco2=total_counterfactual,
            average_savings_percent=avg_savings,
            is_estimate=True,
        )

    def get_tracked_comparisons(self) -> list[CounterfactualComparison]:
        """Get all tracked comparisons.

        Returns:
            List of all tracked CounterfactualComparison objects.
        """
        return list(self._tracked_comparisons)

    def clear_tracked(self) -> None:
        """Clear all tracked comparisons."""
        self._tracked_comparisons.clear()
