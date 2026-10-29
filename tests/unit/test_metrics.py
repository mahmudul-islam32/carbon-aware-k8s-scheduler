"""Unit tests for Prometheus metrics."""

from __future__ import annotations

import pytest
from prometheus_client import REGISTRY

from carbon_scheduler.metrics.prometheus import (
    FORECAST_AGE_SECONDS,
    GRID_RENEWABLE_SHARE,
    JOBS_HELD,
    get_jobs_held_count,
    record_api_request,
    record_co2_avoided,
    record_fallback_release,
    record_job_held,
    record_job_released,
    reset_jobs_held,
    update_forecast_age,
    update_renewable_share,
)


@pytest.fixture(autouse=True)
def reset_metrics() -> None:
    """Reset metrics before each test to ensure isolation.

    Note: In production tests, you might use a separate registry per test.
    Here we clear label values to reset state.
    """
    # Clear gauge values for common test namespaces
    for ns in ["default", "test-ns", "production"]:
        JOBS_HELD.labels(namespace=ns).set(0)

    GRID_RENEWABLE_SHARE.set(0)
    FORECAST_AGE_SECONDS.set(0)


class TestJobsHeldGauge:
    """Tests for the carbon_jobs_held gauge metric."""

    def test_increment_jobs_held(self) -> None:
        """Test incrementing jobs held count."""
        record_job_held("default")
        assert get_jobs_held_count("default") == 1.0

        record_job_held("default")
        assert get_jobs_held_count("default") == 2.0

    def test_jobs_held_per_namespace(self) -> None:
        """Test that jobs held are tracked per namespace."""
        record_job_held("default")
        record_job_held("default")
        record_job_held("production")

        assert get_jobs_held_count("default") == 2.0
        assert get_jobs_held_count("production") == 1.0

    def test_reset_jobs_held(self) -> None:
        """Test resetting jobs held count."""
        record_job_held("default")
        record_job_held("default")
        assert get_jobs_held_count("default") == 2.0

        reset_jobs_held("default")
        assert get_jobs_held_count("default") == 0.0

    def test_decrement_on_release(self) -> None:
        """Test that releasing a job decrements held count."""
        record_job_held("default")
        record_job_held("default")
        assert get_jobs_held_count("default") == 2.0

        record_job_released("default", "my-job")
        assert get_jobs_held_count("default") == 1.0


class TestJobDelayHistogram:
    """Tests for the carbon_job_delay_seconds histogram metric."""

    def test_observe_delay(self) -> None:
        """Test observing job delays."""
        record_job_held("test-ns")
        record_job_released("test-ns", "job-1", delay_seconds=300.0)

        # Check histogram was updated - the metric should have been observed at least once
        count = REGISTRY.get_sample_value(
            "carbon_job_delay_seconds_count",
            {"namespace": "test-ns", "job_name": "job-1"},
        )
        assert count is not None and count >= 1.0

    def test_delay_buckets(self) -> None:
        """Test that delays fall into correct histogram buckets."""
        record_job_held("test-ns")
        record_job_released("test-ns", "bucket-test", delay_seconds=120.0)  # 2 minutes

        # Should be in the 300s bucket (5 min) but not in 60s bucket
        bucket_60 = REGISTRY.get_sample_value(
            "carbon_job_delay_seconds_bucket",
            {"namespace": "test-ns", "job_name": "bucket-test", "le": "60.0"},
        )
        bucket_300 = REGISTRY.get_sample_value(
            "carbon_job_delay_seconds_bucket",
            {"namespace": "test-ns", "job_name": "bucket-test", "le": "300.0"},
        )
        assert bucket_60 == 0.0  # 120s > 60s bucket
        assert bucket_300 == 1.0  # 120s <= 300s bucket

    def test_release_without_delay(self) -> None:
        """Test releasing a job without recording delay."""
        record_job_held("test-ns")
        record_job_released("test-ns", "no-delay-job")

        # Released counter should increment
        count = REGISTRY.get_sample_value(
            "carbon_jobs_released_total",
            {"namespace": "test-ns", "job_name": "no-delay-job"},
        )
        assert count == 1.0


class TestCO2Counter:
    """Tests for the carbon_co2_avoided_grams_total counter metric."""

    def test_increment_co2_avoided(self) -> None:
        """Test incrementing CO2 avoided."""
        record_co2_avoided("default", "etl-job", 500.0)

        count = REGISTRY.get_sample_value(
            "carbon_co2_avoided_grams_total",
            {"namespace": "default", "job_name": "etl-job"},
        )
        assert count == 500.0

    def test_cumulative_co2(self) -> None:
        """Test that CO2 avoided accumulates over multiple recordings."""
        record_co2_avoided("default", "batch-job", 100.0)
        record_co2_avoided("default", "batch-job", 250.0)
        record_co2_avoided("default", "batch-job", 150.0)

        count = REGISTRY.get_sample_value(
            "carbon_co2_avoided_grams_total",
            {"namespace": "default", "job_name": "batch-job"},
        )
        assert count == 500.0

    def test_co2_per_job(self) -> None:
        """Test that CO2 is tracked per job."""
        record_co2_avoided("default", "job-a", 100.0)
        record_co2_avoided("default", "job-b", 200.0)

        count_a = REGISTRY.get_sample_value(
            "carbon_co2_avoided_grams_total",
            {"namespace": "default", "job_name": "job-a"},
        )
        count_b = REGISTRY.get_sample_value(
            "carbon_co2_avoided_grams_total",
            {"namespace": "default", "job_name": "job-b"},
        )
        assert count_a == 100.0
        assert count_b == 200.0


class TestRenewableShareGauge:
    """Tests for the carbon_grid_renewable_share_percent gauge metric."""

    def test_update_renewable_share(self) -> None:
        """Test updating renewable share."""
        update_renewable_share(65.5)

        value = REGISTRY.get_sample_value("carbon_grid_renewable_share_percent")
        assert value == 65.5

    def test_renewable_share_overwrite(self) -> None:
        """Test that renewable share is overwritten, not accumulated."""
        update_renewable_share(50.0)
        update_renewable_share(75.0)

        value = REGISTRY.get_sample_value("carbon_grid_renewable_share_percent")
        assert value == 75.0


class TestFallbackReleasesCounter:
    """Tests for the fallback_releases_total counter metric."""

    def test_increment_fallback(self) -> None:
        """Test incrementing fallback releases."""
        record_fallback_release("default", "urgent-job")

        count = REGISTRY.get_sample_value(
            "fallback_releases_total",
            {"namespace": "default", "job_name": "urgent-job"},
        )
        assert count == 1.0

    def test_multiple_fallbacks(self) -> None:
        """Test multiple fallback releases accumulate."""
        record_fallback_release("default", "retry-job")
        record_fallback_release("default", "retry-job")
        record_fallback_release("default", "retry-job")

        count = REGISTRY.get_sample_value(
            "fallback_releases_total",
            {"namespace": "default", "job_name": "retry-job"},
        )
        assert count == 3.0


class TestAPIMetrics:
    """Tests for API request metrics."""

    def test_record_api_request_success(self) -> None:
        """Test recording successful API request."""
        record_api_request("ren_share_forecast", "success", latency_seconds=0.15)

        count = REGISTRY.get_sample_value(
            "carbon_api_requests_total",
            {"endpoint": "ren_share_forecast", "status": "success"},
        )
        assert count == 1.0

    def test_record_api_request_error(self) -> None:
        """Test recording failed API request."""
        record_api_request("signal", "error")

        count = REGISTRY.get_sample_value(
            "carbon_api_requests_total",
            {"endpoint": "signal", "status": "error"},
        )
        assert count == 1.0

    def test_api_latency_histogram(self) -> None:
        """Test API latency histogram observation."""
        # Use an endpoint label no other test (or the real grid client) records
        record_api_request("latency_test", "success", latency_seconds=0.25)

        count = REGISTRY.get_sample_value(
            "carbon_api_latency_seconds_count",
            {"endpoint": "latency_test"},
        )
        assert count == 1.0

        # Check it falls in correct bucket
        bucket_250ms = REGISTRY.get_sample_value(
            "carbon_api_latency_seconds_bucket",
            {"endpoint": "latency_test", "le": "0.25"},
        )
        assert bucket_250ms == 1.0

    def test_api_request_without_latency(self) -> None:
        """Test recording API request without latency."""
        record_api_request("health", "success")

        count = REGISTRY.get_sample_value(
            "carbon_api_requests_total",
            {"endpoint": "health", "status": "success"},
        )
        assert count == 1.0

    def test_api_labels_separate(self) -> None:
        """Test that different endpoints/statuses are tracked separately."""
        # Get baseline values first (counters persist across tests)
        baseline_success = REGISTRY.get_sample_value(
            "carbon_api_requests_total",
            {"endpoint": "api_test_endpoint", "status": "success"},
        ) or 0.0
        baseline_error = REGISTRY.get_sample_value(
            "carbon_api_requests_total",
            {"endpoint": "api_test_endpoint", "status": "error"},
        ) or 0.0
        baseline_other = REGISTRY.get_sample_value(
            "carbon_api_requests_total",
            {"endpoint": "api_test_other", "status": "success"},
        ) or 0.0

        # Use unique endpoint names for this test
        record_api_request("api_test_endpoint", "success")
        record_api_request("api_test_endpoint", "success")
        record_api_request("api_test_endpoint", "error")
        record_api_request("api_test_other", "success")

        success_endpoint = REGISTRY.get_sample_value(
            "carbon_api_requests_total",
            {"endpoint": "api_test_endpoint", "status": "success"},
        )
        error_endpoint = REGISTRY.get_sample_value(
            "carbon_api_requests_total",
            {"endpoint": "api_test_endpoint", "status": "error"},
        )
        success_other = REGISTRY.get_sample_value(
            "carbon_api_requests_total",
            {"endpoint": "api_test_other", "status": "success"},
        )

        assert success_endpoint == baseline_success + 2.0
        assert error_endpoint == baseline_error + 1.0
        assert success_other == baseline_other + 1.0


class TestForecastAgeGauge:
    """Tests for the carbon_forecast_age_seconds gauge metric."""

    def test_update_forecast_age(self) -> None:
        """Test updating forecast age."""
        update_forecast_age(1800.0)  # 30 minutes

        value = REGISTRY.get_sample_value("carbon_forecast_age_seconds")
        assert value == 1800.0

    def test_forecast_age_updates(self) -> None:
        """Test that forecast age is overwritten on update."""
        update_forecast_age(100.0)
        update_forecast_age(200.0)

        value = REGISTRY.get_sample_value("carbon_forecast_age_seconds")
        assert value == 200.0


class TestJobsReleasedCounter:
    """Tests for the carbon_jobs_released_total counter metric."""

    def test_jobs_released_increments(self) -> None:
        """Test that jobs released counter increments."""
        record_job_held("default")
        record_job_released("default", "completed-job")
        record_job_held("default")
        record_job_released("default", "completed-job")

        count = REGISTRY.get_sample_value(
            "carbon_jobs_released_total",
            {"namespace": "default", "job_name": "completed-job"},
        )
        assert count == 2.0


class TestMetricLabels:
    """Tests for metric label behavior."""

    def test_namespace_label_isolation(self) -> None:
        """Test that namespace labels isolate metrics correctly."""
        record_job_held("ns-a")
        record_job_held("ns-a")
        record_job_held("ns-b")

        assert get_jobs_held_count("ns-a") == 2.0
        assert get_jobs_held_count("ns-b") == 1.0

    def test_job_name_label_isolation(self) -> None:
        """Test that job_name labels isolate metrics correctly."""
        record_co2_avoided("default", "job-x", 100.0)
        record_co2_avoided("default", "job-y", 200.0)

        count_x = REGISTRY.get_sample_value(
            "carbon_co2_avoided_grams_total",
            {"namespace": "default", "job_name": "job-x"},
        )
        count_y = REGISTRY.get_sample_value(
            "carbon_co2_avoided_grams_total",
            {"namespace": "default", "job_name": "job-y"},
        )

        assert count_x == 100.0
        assert count_y == 200.0

    def test_special_characters_in_labels(self) -> None:
        """Test labels with special characters work correctly."""
        record_job_held("kube-system")
        record_co2_avoided("my-namespace", "batch-job-v2.1", 50.0)

        assert get_jobs_held_count("kube-system") == 1.0

        count = REGISTRY.get_sample_value(
            "carbon_co2_avoided_grams_total",
            {"namespace": "my-namespace", "job_name": "batch-job-v2.1"},
        )
        assert count == 50.0
