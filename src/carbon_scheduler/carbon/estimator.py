"""Carbon intensity estimation from grid production mix.

This module estimates grid carbon intensity by combining production mix data
from Energy-Charts API with configurable emission factors per production type.

Formula: Carbon Intensity (gCO2/kWh) = Sum(Pi * EFi) / Sum(Pi)
where Pi = production of type i (MW), EFi = emission factor of type i (gCO2/kWh)

All outputs are clearly labeled as ESTIMATES.
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from carbon_scheduler.carbon.models import (
    CarbonIntensityEstimate,
    EmissionFactorConfig,
    ProductionMix,
)


class EmissionFactors:
    """Manages emission factors loaded from a YAML configuration file."""

    def __init__(self, config_path: Path | str | None = None) -> None:
        """Initialize emission factors from config file.

        Args:
            config_path: Path to emission factors YAML file.
                        If None, uses default path from settings.
        """
        self._config: EmissionFactorConfig | None = None
        self._factors: dict[str, float] = {}
        self._default_factor: float = 500.0

        if config_path is not None:
            self.load(config_path)

    def load(self, config_path: Path | str) -> None:
        """Load emission factors from a YAML file.

        Args:
            config_path: Path to the emission factors YAML file.

        Raises:
            FileNotFoundError: If the config file doesn't exist.
            ValueError: If the config file is invalid.
        """
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Emission factors file not found: {path}")

        with path.open() as f:
            raw_config: dict[str, Any] = yaml.safe_load(f)

        self._config = EmissionFactorConfig(
            version=raw_config.get("version", "unknown"),
            country=raw_config.get("country", "unknown"),
            unit=raw_config.get("unit", "gCO2/kWh"),
            emission_factors=raw_config.get("emission_factors", {}),
            default_factor=raw_config.get("default_factor", 500.0),
        )

        self._factors = self._config.emission_factors
        self._default_factor = self._config.default_factor

    @property
    def factors(self) -> dict[str, float]:
        """Get the emission factors dictionary."""
        return self._factors.copy()

    @property
    def default_factor(self) -> float:
        """Get the default emission factor for unknown production types."""
        return self._default_factor

    @property
    def config(self) -> EmissionFactorConfig | None:
        """Get the full configuration object."""
        return self._config

    def get_factor(self, production_type: str) -> float:
        """Get the emission factor for a production type.

        Args:
            production_type: The production type name (e.g., 'Wind Onshore').

        Returns:
            Emission factor in gCO2/kWh, or default_factor if type unknown.
        """
        return self._factors.get(production_type, self._default_factor)

    def has_factor(self, production_type: str) -> bool:
        """Check if a production type has a known emission factor."""
        return production_type in self._factors


class CarbonEstimator:
    """Estimates grid carbon intensity from production mix data.

    The estimator calculates weighted average carbon intensity:
    Carbon Intensity = Sum(Production_i * EmissionFactor_i) / Sum(Production_i)
    """

    def __init__(self, emission_factors: EmissionFactors) -> None:
        """Initialize the carbon estimator.

        Args:
            emission_factors: EmissionFactors instance with loaded factors.
        """
        self._emission_factors = emission_factors

    @property
    def emission_factors(self) -> EmissionFactors:
        """Get the emission factors instance."""
        return self._emission_factors

    def estimate_intensity(
        self,
        production_mix: ProductionMix,
    ) -> CarbonIntensityEstimate:
        """Estimate carbon intensity from a production mix.

        Args:
            production_mix: Production data with power output per type in MW.

        Returns:
            CarbonIntensityEstimate with the weighted average intensity.

        Note:
            If total production is zero, returns 0 intensity.
            Unknown production types use the default emission factor.
        """
        total_production = 0.0
        weighted_emissions = 0.0
        used_types: list[str] = []
        defaulted_types: list[str] = []

        for prod_type, power_mw in production_mix.production.items():
            if power_mw <= 0:
                continue

            total_production += power_mw
            factor = self._emission_factors.get_factor(prod_type)
            weighted_emissions += power_mw * factor
            used_types.append(prod_type)

            if not self._emission_factors.has_factor(prod_type):
                defaulted_types.append(prod_type)

        intensity = 0.0 if total_production == 0 else weighted_emissions / total_production

        return CarbonIntensityEstimate(
            timestamp=production_mix.timestamp,
            carbon_intensity_gco2_kwh=intensity,
            is_estimate=True,
            production_types_used=used_types,
            missing_types_defaulted=defaulted_types,
        )

    def estimate_intensity_for_period(
        self,
        production_mixes: list[ProductionMix],
    ) -> CarbonIntensityEstimate:
        """Estimate average carbon intensity over a period.

        Args:
            production_mixes: List of production mix data points.

        Returns:
            CarbonIntensityEstimate with the time-weighted average intensity.

        Note:
            Returns intensity of 0 if the list is empty.
        """
        if not production_mixes:
            return CarbonIntensityEstimate(
                timestamp=datetime.now(),
                carbon_intensity_gco2_kwh=0.0,
                is_estimate=True,
                production_types_used=[],
                missing_types_defaulted=[],
            )

        estimates = [self.estimate_intensity(mix) for mix in production_mixes]

        avg_intensity = sum(e.carbon_intensity_gco2_kwh for e in estimates) / len(estimates)

        all_used_types: set[str] = set()
        all_defaulted_types: set[str] = set()
        for est in estimates:
            all_used_types.update(est.production_types_used)
            all_defaulted_types.update(est.missing_types_defaulted)

        return CarbonIntensityEstimate(
            timestamp=production_mixes[0].timestamp,
            carbon_intensity_gco2_kwh=avg_intensity,
            is_estimate=True,
            production_types_used=sorted(all_used_types),
            missing_types_defaulted=sorted(all_defaulted_types),
        )

    def estimate_job_energy_kwh(
        self,
        cpu_cores: float,
        gpu_units: float,
        runtime_seconds: float,
        watts_per_cpu_core: float = 10.0,
        watts_per_gpu: float = 200.0,
    ) -> float:
        """Estimate energy consumption for a job.

        Formula: Energy (kWh) = (CPU * W/CPU + GPU * W/GPU) * runtime_hours

        Args:
            cpu_cores: Number of CPU cores requested.
            gpu_units: Number of GPU units requested.
            runtime_seconds: Job runtime in seconds.
            watts_per_cpu_core: Estimated power draw per CPU core.
            watts_per_gpu: Estimated power draw per GPU.

        Returns:
            Estimated energy consumption in kWh (ESTIMATE).
        """
        runtime_hours = runtime_seconds / 3600.0
        total_watts = (cpu_cores * watts_per_cpu_core) + (gpu_units * watts_per_gpu)
        return total_watts * runtime_hours / 1000.0

    def estimate_emissions_gco2(
        self,
        energy_kwh: float,
        carbon_intensity_gco2_kwh: float,
    ) -> float:
        """Estimate CO2 emissions for given energy and intensity.

        Formula: Emissions (gCO2) = Energy (kWh) * Intensity (gCO2/kWh)

        Args:
            energy_kwh: Energy consumption in kWh.
            carbon_intensity_gco2_kwh: Carbon intensity in gCO2/kWh.

        Returns:
            Estimated emissions in grams CO2 (ESTIMATE).
        """
        return energy_kwh * carbon_intensity_gco2_kwh

    def interpolate_intensity(
        self,
        intensities: list[CarbonIntensityEstimate],
        target_time: datetime,
    ) -> CarbonIntensityEstimate:
        """Interpolate carbon intensity for a specific time.

        Args:
            intensities: List of intensity estimates sorted by timestamp.
            target_time: The target time to interpolate for.

        Returns:
            Interpolated CarbonIntensityEstimate for the target time.

        Note:
            Returns the nearest estimate if target_time is outside the range.
            Uses linear interpolation between the two nearest points.
        """
        if not intensities:
            return CarbonIntensityEstimate(
                timestamp=target_time,
                carbon_intensity_gco2_kwh=0.0,
                is_estimate=True,
            )

        if len(intensities) == 1:
            return CarbonIntensityEstimate(
                timestamp=target_time,
                carbon_intensity_gco2_kwh=intensities[0].carbon_intensity_gco2_kwh,
                is_estimate=True,
                production_types_used=intensities[0].production_types_used,
                missing_types_defaulted=intensities[0].missing_types_defaulted,
            )

        sorted_intensities = sorted(intensities, key=lambda x: x.timestamp)

        if target_time <= sorted_intensities[0].timestamp:
            return CarbonIntensityEstimate(
                timestamp=target_time,
                carbon_intensity_gco2_kwh=sorted_intensities[0].carbon_intensity_gco2_kwh,
                is_estimate=True,
                production_types_used=sorted_intensities[0].production_types_used,
                missing_types_defaulted=sorted_intensities[0].missing_types_defaulted,
            )

        if target_time >= sorted_intensities[-1].timestamp:
            return CarbonIntensityEstimate(
                timestamp=target_time,
                carbon_intensity_gco2_kwh=sorted_intensities[-1].carbon_intensity_gco2_kwh,
                is_estimate=True,
                production_types_used=sorted_intensities[-1].production_types_used,
                missing_types_defaulted=sorted_intensities[-1].missing_types_defaulted,
            )

        for i in range(len(sorted_intensities) - 1):
            before = sorted_intensities[i]
            after = sorted_intensities[i + 1]

            if before.timestamp <= target_time <= after.timestamp:
                time_delta = (after.timestamp - before.timestamp).total_seconds()
                if time_delta == 0:
                    weight = 0.5
                else:
                    weight = (target_time - before.timestamp).total_seconds() / time_delta

                interpolated = (
                    before.carbon_intensity_gco2_kwh * (1 - weight)
                    + after.carbon_intensity_gco2_kwh * weight
                )

                all_types = set(before.production_types_used) | set(after.production_types_used)
                all_defaulted = set(before.missing_types_defaulted) | set(
                    after.missing_types_defaulted
                )

                return CarbonIntensityEstimate(
                    timestamp=target_time,
                    carbon_intensity_gco2_kwh=interpolated,
                    is_estimate=True,
                    production_types_used=sorted(all_types),
                    missing_types_defaulted=sorted(all_defaulted),
                )

        return CarbonIntensityEstimate(
            timestamp=target_time,
            carbon_intensity_gco2_kwh=sorted_intensities[-1].carbon_intensity_gco2_kwh,
            is_estimate=True,
            production_types_used=sorted_intensities[-1].production_types_used,
            missing_types_defaulted=sorted_intensities[-1].missing_types_defaulted,
        )

    def estimate_average_intensity_for_window(
        self,
        intensities: list[CarbonIntensityEstimate],
        start_time: datetime,
        duration: timedelta,
        sample_interval: timedelta = timedelta(minutes=15),
    ) -> float:
        """Estimate average carbon intensity over a time window.

        Args:
            intensities: List of intensity estimates.
            start_time: Start of the window.
            duration: Duration of the window.
            sample_interval: Interval for sampling (default 15 minutes).

        Returns:
            Average carbon intensity in gCO2/kWh (ESTIMATE).
        """
        if not intensities or duration.total_seconds() <= 0:
            return 0.0

        samples: list[float] = []
        current_time = start_time
        end_time = start_time + duration

        while current_time < end_time:
            interpolated = self.interpolate_intensity(intensities, current_time)
            samples.append(interpolated.carbon_intensity_gco2_kwh)
            current_time += sample_interval

        if samples:
            return sum(samples) / len(samples)
        return 0.0
