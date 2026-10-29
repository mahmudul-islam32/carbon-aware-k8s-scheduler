"""Unit tests for live CO2 accounting (carbon.live)."""

from datetime import UTC, datetime, timedelta

import pytest

from carbon_scheduler.carbon.live import (
    JobResources,
    estimate_avoided_emissions,
    estimate_energy_kwh,
    extract_job_resources,
    mean_share_for_window,
    parse_cpu_quantity,
    share_to_intensity,
)
from carbon_scheduler.planner.models import Forecast, ForecastPoint

NOW = datetime(2026, 10, 29, 0, 0, tzinfo=UTC)


def _forecast(shares: list[float], step_minutes: int = 60) -> Forecast:
    return Forecast(
        points=[
            ForecastPoint(timestamp=NOW + timedelta(minutes=i * step_minutes), renewable_share_pct=s)
            for i, s in enumerate(shares)
        ]
    )


class TestParseCpuQuantity:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("500m", 0.5), ("2", 2.0), ("1.5", 1.5), (4, 4.0), ("250m", 0.25)],
    )
    def test_valid(self, raw: object, expected: float) -> None:
        assert parse_cpu_quantity(raw) == pytest.approx(expected)

    @pytest.mark.parametrize("raw", [None, "", "lots", "2Gi"])
    def test_invalid_returns_zero(self, raw: object) -> None:
        assert parse_cpu_quantity(raw) == 0.0


class TestExtractJobResources:
    def test_sums_containers_and_parallelism(self) -> None:
        spec = {
            "parallelism": 2,
            "template": {
                "spec": {
                    "containers": [
                        {"resources": {"requests": {"cpu": "2"}}},
                        {"resources": {"requests": {"cpu": "500m", "nvidia.com/gpu": "1"}}},
                    ]
                }
            },
        }
        res = extract_job_resources(spec)
        assert res.cpu_cores == pytest.approx(5.0)
        assert res.gpu_units == pytest.approx(2.0)

    def test_falls_back_to_limits(self) -> None:
        spec = {
            "template": {
                "spec": {"containers": [{"resources": {"limits": {"cpu": "3", "nvidia.com/gpu": 2}}}]}
            }
        }
        res = extract_job_resources(spec)
        assert res.cpu_cores == 3.0
        assert res.gpu_units == 2.0

    def test_defaults_to_one_core(self) -> None:
        assert extract_job_resources(None) == JobResources(cpu_cores=1.0, gpu_units=0.0)
        empty = {"template": {"spec": {"containers": [{"name": "x"}]}}}
        assert extract_job_resources(empty).cpu_cores == 1.0


class TestEnergyAndIntensity:
    def test_energy_kwh(self) -> None:
        # (4 * 10 W + 1 * 200 W) * 2 h = 480 Wh
        res = JobResources(cpu_cores=4, gpu_units=1)
        assert estimate_energy_kwh(res, timedelta(hours=2), 10.0, 200.0) == pytest.approx(0.48)

    def test_share_to_intensity(self) -> None:
        assert share_to_intensity(0, 400) == 400
        assert share_to_intensity(75, 400) == pytest.approx(100)
        assert share_to_intensity(100, 400) == 0
        # Out-of-range shares are clamped
        assert share_to_intensity(120, 400) == 0
        assert share_to_intensity(-5, 400) == 400


class TestMeanShareForWindow:
    def test_mean_inside_window(self) -> None:
        fc = _forecast([40, 60, 80])
        assert mean_share_for_window(fc, NOW, timedelta(hours=1)) == pytest.approx(50)

    def test_short_window_uses_previous_point(self) -> None:
        fc = _forecast([40, 60, 80])
        start = NOW + timedelta(minutes=10)
        assert mean_share_for_window(fc, start, timedelta(minutes=20)) == 40

    def test_uncovered_window(self) -> None:
        fc = _forecast([40, 60])
        assert mean_share_for_window(fc, NOW - timedelta(hours=5), timedelta(minutes=5)) is None


class TestEstimateAvoidedEmissions:
    def test_hand_computed_example(self) -> None:
        # 1 core * 10 W * 1 h = 0.01 kWh
        # baseline window [00:00, 01:00] -> mean(20, 40) = 30 % -> 280 g/kWh -> 2.8 g
        # planned  window [02:00, 03:00] -> mean(80, 80) = 80 % ->  80 g/kWh -> 0.8 g
        fc = _forecast([20, 40, 80, 80])
        result = estimate_avoided_emissions(
            forecast=fc,
            submit_time=NOW,
            planned_start=NOW + timedelta(hours=2),
            runtime=timedelta(hours=1),
            resources=JobResources(cpu_cores=1, gpu_units=0),
        )
        assert result is not None
        assert result.energy_kwh == pytest.approx(0.01)
        assert result.baseline_gco2 == pytest.approx(2.8)
        assert result.planned_gco2 == pytest.approx(0.8)
        assert result.avoided_gco2 == pytest.approx(2.0)

    def test_returns_none_without_coverage(self) -> None:
        fc = _forecast([50])
        result = estimate_avoided_emissions(
            forecast=fc,
            submit_time=NOW - timedelta(days=1),
            planned_start=NOW,
            runtime=timedelta(minutes=30),
            resources=JobResources(cpu_cores=1, gpu_units=0),
        )
        assert result is None
