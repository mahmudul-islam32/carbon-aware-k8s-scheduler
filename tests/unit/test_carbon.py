"""Unit tests for carbon intensity estimation and accounting.

Tests cover:
- Emission factor loading from YAML
- Carbon intensity calculation with hand-computed examples
- Counterfactual comparison
- Edge cases (missing data, unknown production types)
"""

from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from carbon_scheduler.carbon.accounting import CarbonAccounting, JobEmissions
from carbon_scheduler.carbon.estimator import CarbonEstimator, EmissionFactors
from carbon_scheduler.carbon.models import (
    CarbonIntensityEstimate,
    ProductionMix,
)


class TestEmissionFactors:
    """Tests for EmissionFactors class."""

    def test_load_emission_factors_from_yaml(self, tmp_path: Path) -> None:
        """Test loading emission factors from a valid YAML file."""
        config = {
            "version": "1.0.0",
            "country": "de",
            "unit": "gCO2/kWh",
            "emission_factors": {
                "Wind Onshore": 11,
                "Solar": 45,
                "Lignite": 1054,
                "Natural Gas": 490,
            },
            "default_factor": 500,
        }
        config_file = tmp_path / "emission_factors.yaml"
        with config_file.open("w") as f:
            yaml.dump(config, f)

        factors = EmissionFactors(config_file)

        assert factors.get_factor("Wind Onshore") == 11
        assert factors.get_factor("Solar") == 45
        assert factors.get_factor("Lignite") == 1054
        assert factors.get_factor("Natural Gas") == 490
        assert factors.default_factor == 500

    def test_load_from_real_config(self) -> None:
        """Test loading the actual emission_factors.yaml config file."""
        config_path = Path("config/emission_factors.yaml")
        if not config_path.exists():
            pytest.skip("Config file not found")

        factors = EmissionFactors(config_path)

        assert factors.config is not None
        assert factors.config.version == "1.0.0"
        assert factors.config.country == "de"
        assert factors.get_factor("Lignite") == 1054
        assert factors.get_factor("Wind Onshore") == 11
        assert factors.get_factor("Solar") == 45

    def test_emission_factors_not_found(self) -> None:
        """Test error when emission factors file doesn't exist."""
        with pytest.raises(FileNotFoundError, match="not found"):
            EmissionFactors("/nonexistent/path.yaml")

    def test_get_factor_unknown_type_uses_default(self, tmp_path: Path) -> None:
        """Test that unknown production types use the default factor."""
        config = {
            "version": "1.0.0",
            "country": "de",
            "emission_factors": {"Solar": 45},
            "default_factor": 999,
        }
        config_file = tmp_path / "factors.yaml"
        with config_file.open("w") as f:
            yaml.dump(config, f)

        factors = EmissionFactors(config_file)

        assert factors.get_factor("Unknown Type") == 999
        assert factors.has_factor("Solar") is True
        assert factors.has_factor("Unknown Type") is False

    def test_factors_property_returns_copy(self, tmp_path: Path) -> None:
        """Test that factors property returns a copy, not the original."""
        config = {
            "version": "1.0.0",
            "country": "de",
            "emission_factors": {"Solar": 45},
            "default_factor": 500,
        }
        config_file = tmp_path / "factors.yaml"
        with config_file.open("w") as f:
            yaml.dump(config, f)

        factors = EmissionFactors(config_file)
        factors_dict = factors.factors
        factors_dict["Modified"] = 1000

        assert "Modified" not in factors.factors


class TestCarbonEstimator:
    """Tests for CarbonEstimator class."""

    @pytest.fixture
    def emission_factors(self, tmp_path: Path) -> EmissionFactors:
        """Create emission factors fixture."""
        config = {
            "version": "1.0.0",
            "country": "de",
            "unit": "gCO2/kWh",
            "emission_factors": {
                "Wind Onshore": 11,
                "Solar": 45,
                "Lignite": 1054,
                "Natural Gas": 490,
            },
            "default_factor": 500,
        }
        config_file = tmp_path / "emission_factors.yaml"
        with config_file.open("w") as f:
            yaml.dump(config, f)
        return EmissionFactors(config_file)

    @pytest.fixture
    def estimator(self, emission_factors: EmissionFactors) -> CarbonEstimator:
        """Create carbon estimator fixture."""
        return CarbonEstimator(emission_factors)

    def test_estimate_intensity_single_type(self, estimator: CarbonEstimator) -> None:
        """Test intensity calculation with a single production type.

        Hand-computed: 100% wind at 11 gCO2/kWh = 11 gCO2/kWh
        """
        mix = ProductionMix(
            timestamp=datetime(2026, 10, 29, 12, 0, 0),
            production={"Wind Onshore": 1000},
        )

        result = estimator.estimate_intensity(mix)

        assert result.carbon_intensity_gco2_kwh == 11.0
        assert result.is_estimate is True
        assert "Wind Onshore" in result.production_types_used
        assert len(result.missing_types_defaulted) == 0

    def test_estimate_intensity_mixed_production(self, estimator: CarbonEstimator) -> None:
        """Test intensity calculation with mixed production.

        Hand-computed example:
        - Wind: 500 MW * 11 gCO2/kWh = 5,500 gCO2/kWh*MW
        - Lignite: 500 MW * 1054 gCO2/kWh = 527,000 gCO2/kWh*MW
        - Total: 1000 MW
        - Weighted average: (5500 + 527000) / 1000 = 532.5 gCO2/kWh
        """
        mix = ProductionMix(
            timestamp=datetime(2026, 10, 29, 12, 0, 0),
            production={"Wind Onshore": 500, "Lignite": 500},
        )

        result = estimator.estimate_intensity(mix)

        expected = (500 * 11 + 500 * 1054) / 1000
        assert result.carbon_intensity_gco2_kwh == pytest.approx(expected)
        assert result.carbon_intensity_gco2_kwh == pytest.approx(532.5)

    def test_estimate_intensity_complex_mix(self, estimator: CarbonEstimator) -> None:
        """Test intensity with a more complex production mix.

        Hand-computed:
        - Wind: 300 MW * 11 = 3,300
        - Solar: 200 MW * 45 = 9,000
        - Natural Gas: 400 MW * 490 = 196,000
        - Lignite: 100 MW * 1054 = 105,400
        - Total: 1000 MW
        - Sum: 313,700
        - Average: 313.7 gCO2/kWh
        """
        mix = ProductionMix(
            timestamp=datetime(2026, 10, 29, 12, 0, 0),
            production={
                "Wind Onshore": 300,
                "Solar": 200,
                "Natural Gas": 400,
                "Lignite": 100,
            },
        )

        result = estimator.estimate_intensity(mix)

        expected = (300 * 11 + 200 * 45 + 400 * 490 + 100 * 1054) / 1000
        assert result.carbon_intensity_gco2_kwh == pytest.approx(expected)
        assert result.carbon_intensity_gco2_kwh == pytest.approx(313.7)

    def test_estimate_intensity_with_unknown_type(self, estimator: CarbonEstimator) -> None:
        """Test that unknown production types use default factor.

        Hand-computed:
        - Wind: 500 MW * 11 = 5,500
        - Unknown: 500 MW * 500 (default) = 250,000
        - Total: 1000 MW
        - Average: 255,500 / 1000 = 255.5 gCO2/kWh
        """
        mix = ProductionMix(
            timestamp=datetime(2026, 10, 29, 12, 0, 0),
            production={"Wind Onshore": 500, "New Energy Type": 500},
        )

        result = estimator.estimate_intensity(mix)

        expected = (500 * 11 + 500 * 500) / 1000
        assert result.carbon_intensity_gco2_kwh == pytest.approx(expected)
        assert result.carbon_intensity_gco2_kwh == pytest.approx(255.5)
        assert "New Energy Type" in result.missing_types_defaulted

    def test_estimate_intensity_zero_production(self, estimator: CarbonEstimator) -> None:
        """Test intensity calculation with zero total production."""
        mix = ProductionMix(
            timestamp=datetime(2026, 10, 29, 12, 0, 0),
            production={},
        )

        result = estimator.estimate_intensity(mix)

        assert result.carbon_intensity_gco2_kwh == 0.0

    def test_estimate_intensity_negative_values_ignored(
        self, estimator: CarbonEstimator
    ) -> None:
        """Test that negative production values are ignored."""
        mix = ProductionMix(
            timestamp=datetime(2026, 10, 29, 12, 0, 0),
            production={"Wind Onshore": 1000, "Solar": -100},
        )

        result = estimator.estimate_intensity(mix)

        assert result.carbon_intensity_gco2_kwh == 11.0
        assert "Solar" not in result.production_types_used

    def test_estimate_job_energy(self, estimator: CarbonEstimator) -> None:
        """Test job energy estimation.

        Hand-computed:
        - 4 CPU cores * 10 W/core = 40 W
        - 1 GPU * 200 W = 200 W
        - Total: 240 W
        - Runtime: 1 hour (3600 seconds)
        - Energy: 240 W * 1 h / 1000 = 0.24 kWh
        """
        energy = estimator.estimate_job_energy_kwh(
            cpu_cores=4,
            gpu_units=1,
            runtime_seconds=3600,
            watts_per_cpu_core=10,
            watts_per_gpu=200,
        )

        assert energy == pytest.approx(0.24)

    def test_estimate_emissions(self, estimator: CarbonEstimator) -> None:
        """Test emissions estimation.

        Hand-computed:
        - Energy: 0.24 kWh
        - Intensity: 500 gCO2/kWh
        - Emissions: 0.24 * 500 = 120 gCO2
        """
        emissions = estimator.estimate_emissions_gco2(
            energy_kwh=0.24,
            carbon_intensity_gco2_kwh=500,
        )

        assert emissions == pytest.approx(120)

    def test_estimate_intensity_for_period(self, estimator: CarbonEstimator) -> None:
        """Test average intensity over multiple time points."""
        mixes = [
            ProductionMix(
                timestamp=datetime(2026, 10, 29, 12, 0, 0),
                production={"Wind Onshore": 1000},
            ),
            ProductionMix(
                timestamp=datetime(2026, 10, 29, 13, 0, 0),
                production={"Lignite": 1000},
            ),
        ]

        result = estimator.estimate_intensity_for_period(mixes)

        expected = (11 + 1054) / 2
        assert result.carbon_intensity_gco2_kwh == pytest.approx(expected)

    def test_estimate_intensity_for_period_empty(self, estimator: CarbonEstimator) -> None:
        """Test average intensity with empty list."""
        result = estimator.estimate_intensity_for_period([])

        assert result.carbon_intensity_gco2_kwh == 0.0

    def test_interpolate_intensity_between_points(self, estimator: CarbonEstimator) -> None:
        """Test linear interpolation between two intensity points."""
        intensities = [
            CarbonIntensityEstimate(
                timestamp=datetime(2026, 10, 29, 12, 0, 0),
                carbon_intensity_gco2_kwh=100,
            ),
            CarbonIntensityEstimate(
                timestamp=datetime(2026, 10, 29, 14, 0, 0),
                carbon_intensity_gco2_kwh=200,
            ),
        ]

        result = estimator.interpolate_intensity(
            intensities,
            datetime(2026, 10, 29, 13, 0, 0),
        )

        assert result.carbon_intensity_gco2_kwh == pytest.approx(150)

    def test_interpolate_intensity_before_range(self, estimator: CarbonEstimator) -> None:
        """Test interpolation before the data range uses first value."""
        intensities = [
            CarbonIntensityEstimate(
                timestamp=datetime(2026, 10, 29, 12, 0, 0),
                carbon_intensity_gco2_kwh=100,
            ),
            CarbonIntensityEstimate(
                timestamp=datetime(2026, 10, 29, 14, 0, 0),
                carbon_intensity_gco2_kwh=200,
            ),
        ]

        result = estimator.interpolate_intensity(
            intensities,
            datetime(2026, 10, 29, 10, 0, 0),
        )

        assert result.carbon_intensity_gco2_kwh == 100

    def test_interpolate_intensity_after_range(self, estimator: CarbonEstimator) -> None:
        """Test interpolation after the data range uses last value."""
        intensities = [
            CarbonIntensityEstimate(
                timestamp=datetime(2026, 10, 29, 12, 0, 0),
                carbon_intensity_gco2_kwh=100,
            ),
            CarbonIntensityEstimate(
                timestamp=datetime(2026, 10, 29, 14, 0, 0),
                carbon_intensity_gco2_kwh=200,
            ),
        ]

        result = estimator.interpolate_intensity(
            intensities,
            datetime(2026, 10, 29, 16, 0, 0),
        )

        assert result.carbon_intensity_gco2_kwh == 200


class TestJobEmissions:
    """Tests for JobEmissions class."""

    @pytest.fixture
    def job_emissions(self, tmp_path: Path) -> JobEmissions:
        """Create JobEmissions fixture."""
        config = {
            "version": "1.0.0",
            "country": "de",
            "emission_factors": {"Wind Onshore": 11, "Lignite": 1054},
            "default_factor": 500,
        }
        config_file = tmp_path / "factors.yaml"
        with config_file.open("w") as f:
            yaml.dump(config, f)

        factors = EmissionFactors(config_file)
        estimator = CarbonEstimator(factors)
        return JobEmissions(estimator, watts_per_cpu_core=10, watts_per_gpu=200)

    def test_estimate_energy(self, job_emissions: JobEmissions) -> None:
        """Test job energy estimation."""
        result = job_emissions.estimate_energy(
            job_name="test-job",
            job_namespace="default",
            cpu_cores=4,
            gpu_units=1,
            runtime_seconds=3600,
        )

        assert result.job_name == "test-job"
        assert result.job_namespace == "default"
        assert result.estimated_energy_kwh == pytest.approx(0.24)
        assert result.is_estimate is True

    def test_estimate_emissions(self, job_emissions: JobEmissions) -> None:
        """Test job emissions estimation with intensity data."""
        intensities = [
            CarbonIntensityEstimate(
                timestamp=datetime(2026, 10, 29, 12, 0, 0),
                carbon_intensity_gco2_kwh=500,
            ),
            CarbonIntensityEstimate(
                timestamp=datetime(2026, 10, 29, 14, 0, 0),
                carbon_intensity_gco2_kwh=500,
            ),
        ]

        result = job_emissions.estimate_emissions(
            job_name="test-job",
            job_namespace="default",
            cpu_cores=4,
            gpu_units=1,
            runtime_seconds=3600,
            start_time=datetime(2026, 10, 29, 12, 0, 0),
            intensities=intensities,
        )

        assert result.estimated_energy_kwh == pytest.approx(0.24)
        assert result.estimated_emissions_gco2 == pytest.approx(120)
        assert result.is_estimate is True


class TestCarbonAccounting:
    """Tests for CarbonAccounting class."""

    @pytest.fixture
    def accounting(self, tmp_path: Path) -> CarbonAccounting:
        """Create CarbonAccounting fixture."""
        config = {
            "version": "1.0.0",
            "country": "de",
            "emission_factors": {"Wind Onshore": 11, "Lignite": 1054},
            "default_factor": 500,
        }
        config_file = tmp_path / "factors.yaml"
        with config_file.open("w") as f:
            yaml.dump(config, f)

        factors = EmissionFactors(config_file)
        estimator = CarbonEstimator(factors)
        return CarbonAccounting(estimator, watts_per_cpu_core=10, watts_per_gpu=200)

    def test_compare_counterfactual_savings(self, accounting: CarbonAccounting) -> None:
        """Test counterfactual comparison shows savings when delayed to green period.

        Scenario:
        - Job submitted at 12:00 when intensity is 1000 gCO2/kWh (high carbon)
        - Job starts at 14:00 when intensity is 100 gCO2/kWh (low carbon)
        - Should show positive avoided emissions
        """
        intensities = [
            CarbonIntensityEstimate(
                timestamp=datetime(2026, 10, 29, 12, 0, 0),
                carbon_intensity_gco2_kwh=1000,
            ),
            CarbonIntensityEstimate(
                timestamp=datetime(2026, 10, 29, 14, 0, 0),
                carbon_intensity_gco2_kwh=100,
            ),
        ]

        result = accounting.compare_counterfactual(
            job_name="test-job",
            job_namespace="default",
            cpu_cores=4,
            gpu_units=0,
            runtime_seconds=3600,
            submit_time=datetime(2026, 10, 29, 12, 0, 0),
            actual_start_time=datetime(2026, 10, 29, 14, 0, 0),
            intensities=intensities,
        )

        assert result.avoided_emissions_gco2 > 0
        assert result.savings_percent > 0
        assert result.counterfactual_emissions_gco2 > result.actual_emissions_gco2
        assert result.is_estimate is True

    def test_compare_counterfactual_no_savings(self, accounting: CarbonAccounting) -> None:
        """Test counterfactual comparison when delayed to higher carbon period.

        Scenario:
        - Job submitted at 12:00 when intensity is 100 gCO2/kWh
        - Job starts at 14:00 when intensity is 1000 gCO2/kWh
        - Should show negative avoided emissions (delay was worse)
        """
        intensities = [
            CarbonIntensityEstimate(
                timestamp=datetime(2026, 10, 29, 12, 0, 0),
                carbon_intensity_gco2_kwh=100,
            ),
            CarbonIntensityEstimate(
                timestamp=datetime(2026, 10, 29, 14, 0, 0),
                carbon_intensity_gco2_kwh=1000,
            ),
        ]

        result = accounting.compare_counterfactual(
            job_name="test-job",
            job_namespace="default",
            cpu_cores=4,
            gpu_units=0,
            runtime_seconds=3600,
            submit_time=datetime(2026, 10, 29, 12, 0, 0),
            actual_start_time=datetime(2026, 10, 29, 14, 0, 0),
            intensities=intensities,
        )

        assert result.avoided_emissions_gco2 < 0
        assert result.savings_percent < 0

    def test_compare_counterfactual_immediate_start(self, accounting: CarbonAccounting) -> None:
        """Test counterfactual when job starts immediately (no delay)."""
        intensities = [
            CarbonIntensityEstimate(
                timestamp=datetime(2026, 10, 29, 12, 0, 0),
                carbon_intensity_gco2_kwh=500,
            ),
        ]

        result = accounting.compare_counterfactual(
            job_name="test-job",
            job_namespace="default",
            cpu_cores=4,
            gpu_units=0,
            runtime_seconds=3600,
            submit_time=datetime(2026, 10, 29, 12, 0, 0),
            actual_start_time=datetime(2026, 10, 29, 12, 0, 0),
            intensities=intensities,
        )

        assert result.avoided_emissions_gco2 == pytest.approx(0)
        assert result.actual_emissions_gco2 == result.counterfactual_emissions_gco2

    def test_cumulative_savings_tracking(self, accounting: CarbonAccounting) -> None:
        """Test cumulative savings across multiple jobs."""
        intensities = [
            CarbonIntensityEstimate(
                timestamp=datetime(2026, 10, 29, 12, 0, 0),
                carbon_intensity_gco2_kwh=1000,
            ),
            CarbonIntensityEstimate(
                timestamp=datetime(2026, 10, 29, 14, 0, 0),
                carbon_intensity_gco2_kwh=100,
            ),
        ]

        for i in range(3):
            accounting.compare_and_track(
                job_name=f"test-job-{i}",
                job_namespace="default",
                cpu_cores=4,
                gpu_units=0,
                runtime_seconds=3600,
                submit_time=datetime(2026, 10, 29, 12, 0, 0),
                actual_start_time=datetime(2026, 10, 29, 14, 0, 0),
                intensities=intensities,
            )

        savings = accounting.get_cumulative_savings()

        assert savings.total_jobs == 3
        assert savings.total_avoided_gco2 > 0
        assert savings.total_avoided_kg_co2 == pytest.approx(savings.total_avoided_gco2 / 1000)
        assert savings.average_savings_percent > 0
        assert savings.is_estimate is True

    def test_cumulative_savings_empty(self, accounting: CarbonAccounting) -> None:
        """Test cumulative savings with no tracked jobs."""
        savings = accounting.get_cumulative_savings()

        assert savings.total_jobs == 0
        assert savings.total_avoided_gco2 == 0
        assert savings.average_savings_percent == 0

    def test_clear_tracked(self, accounting: CarbonAccounting) -> None:
        """Test clearing tracked comparisons."""
        intensities = [
            CarbonIntensityEstimate(
                timestamp=datetime(2026, 10, 29, 12, 0, 0),
                carbon_intensity_gco2_kwh=500,
            ),
        ]

        accounting.compare_and_track(
            job_name="test-job",
            job_namespace="default",
            cpu_cores=4,
            gpu_units=0,
            runtime_seconds=3600,
            submit_time=datetime(2026, 10, 29, 12, 0, 0),
            actual_start_time=datetime(2026, 10, 29, 12, 0, 0),
            intensities=intensities,
        )

        assert len(accounting.get_tracked_comparisons()) == 1

        accounting.clear_tracked()

        assert len(accounting.get_tracked_comparisons()) == 0
        assert accounting.get_cumulative_savings().total_jobs == 0


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    @pytest.fixture
    def emission_factors(self, tmp_path: Path) -> EmissionFactors:
        """Create emission factors fixture."""
        config = {
            "version": "1.0.0",
            "country": "de",
            "emission_factors": {"Wind Onshore": 11},
            "default_factor": 500,
        }
        config_file = tmp_path / "factors.yaml"
        with config_file.open("w") as f:
            yaml.dump(config, f)
        return EmissionFactors(config_file)

    def test_empty_production_mix(self, emission_factors: EmissionFactors) -> None:
        """Test handling of empty production mix."""
        estimator = CarbonEstimator(emission_factors)
        mix = ProductionMix(
            timestamp=datetime(2026, 10, 29, 12, 0, 0),
            production={},
        )

        result = estimator.estimate_intensity(mix)

        assert result.carbon_intensity_gco2_kwh == 0.0
        assert len(result.production_types_used) == 0

    def test_all_unknown_production_types(self, emission_factors: EmissionFactors) -> None:
        """Test handling when all production types are unknown."""
        estimator = CarbonEstimator(emission_factors)
        mix = ProductionMix(
            timestamp=datetime(2026, 10, 29, 12, 0, 0),
            production={"Unknown A": 500, "Unknown B": 500},
        )

        result = estimator.estimate_intensity(mix)

        assert result.carbon_intensity_gco2_kwh == 500.0
        assert len(result.missing_types_defaulted) == 2

    def test_interpolate_single_point(self, emission_factors: EmissionFactors) -> None:
        """Test interpolation with only one data point."""
        estimator = CarbonEstimator(emission_factors)
        intensities = [
            CarbonIntensityEstimate(
                timestamp=datetime(2026, 10, 29, 12, 0, 0),
                carbon_intensity_gco2_kwh=500,
            ),
        ]

        result = estimator.interpolate_intensity(
            intensities,
            datetime(2026, 10, 29, 14, 0, 0),
        )

        assert result.carbon_intensity_gco2_kwh == 500

    def test_interpolate_empty_list(self, emission_factors: EmissionFactors) -> None:
        """Test interpolation with empty intensity list."""
        estimator = CarbonEstimator(emission_factors)

        result = estimator.interpolate_intensity(
            [],
            datetime(2026, 10, 29, 12, 0, 0),
        )

        assert result.carbon_intensity_gco2_kwh == 0.0

    def test_zero_runtime_job(self, emission_factors: EmissionFactors) -> None:
        """Test job energy estimation with zero runtime."""
        estimator = CarbonEstimator(emission_factors)

        energy = estimator.estimate_job_energy_kwh(
            cpu_cores=4,
            gpu_units=1,
            runtime_seconds=0,
        )

        assert energy == 0.0

    def test_zero_resources_job(self, emission_factors: EmissionFactors) -> None:
        """Test job energy estimation with zero resources."""
        estimator = CarbonEstimator(emission_factors)

        energy = estimator.estimate_job_energy_kwh(
            cpu_cores=0,
            gpu_units=0,
            runtime_seconds=3600,
        )

        assert energy == 0.0

    def test_very_large_production_values(self, emission_factors: EmissionFactors) -> None:
        """Test handling of very large production values."""
        estimator = CarbonEstimator(emission_factors)
        mix = ProductionMix(
            timestamp=datetime(2026, 10, 29, 12, 0, 0),
            production={"Wind Onshore": 1_000_000_000},
        )

        result = estimator.estimate_intensity(mix)

        assert result.carbon_intensity_gco2_kwh == 11.0

    def test_average_intensity_for_window_zero_duration(
        self, emission_factors: EmissionFactors
    ) -> None:
        """Test average intensity with zero duration window."""
        estimator = CarbonEstimator(emission_factors)
        intensities = [
            CarbonIntensityEstimate(
                timestamp=datetime(2026, 10, 29, 12, 0, 0),
                carbon_intensity_gco2_kwh=500,
            ),
        ]

        result = estimator.estimate_average_intensity_for_window(
            intensities=intensities,
            start_time=datetime(2026, 10, 29, 12, 0, 0),
            duration=timedelta(seconds=0),
        )

        assert result == 0.0
