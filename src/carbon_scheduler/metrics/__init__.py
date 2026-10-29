"""Prometheus metrics module."""

from carbon_scheduler.metrics.prometheus import (
    API_LATENCY_SECONDS,
    API_REQUESTS,
    CO2_AVOIDED_GRAMS,
    FALLBACK_RELEASES,
    FORECAST_AGE_SECONDS,
    GRID_RENEWABLE_SHARE,
    JOB_DELAY_SECONDS,
    JOBS_HELD,
    JOBS_RELEASED,
    get_jobs_held_count,
    record_api_request,
    record_co2_avoided,
    record_fallback_release,
    record_job_held,
    record_job_released,
    reset_jobs_held,
    setup_metrics,
    update_forecast_age,
    update_renewable_share,
)

__all__ = [
    # Core metrics (F-07)
    "JOBS_HELD",
    "JOB_DELAY_SECONDS",
    "CO2_AVOIDED_GRAMS",
    "GRID_RENEWABLE_SHARE",
    "FALLBACK_RELEASES",
    # Additional metrics
    "JOBS_RELEASED",
    "API_REQUESTS",
    "API_LATENCY_SECONDS",
    "FORECAST_AGE_SECONDS",
    # Setup function
    "setup_metrics",
    # Helper functions
    "record_job_held",
    "record_job_released",
    "record_fallback_release",
    "record_co2_avoided",
    "update_renewable_share",
    "record_api_request",
    "update_forecast_age",
    "get_jobs_held_count",
    "reset_jobs_held",
]
