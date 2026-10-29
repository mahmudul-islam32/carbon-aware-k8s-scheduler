"""Prometheus metrics for the Carbon-Aware Kubernetes Batch Scheduler.

This module defines all Prometheus metrics exposed by the scheduler and provides
helper functions for updating them consistently.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from prometheus_client import (
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    start_http_server,
)

if TYPE_CHECKING:
    from prometheus_client.registry import CollectorRegistry

logger = logging.getLogger(__name__)

# Default port for metrics endpoint
DEFAULT_METRICS_PORT = 8000

# Common label names
LABEL_NAMESPACE = "namespace"
LABEL_JOB_NAME = "job_name"
LABEL_ENDPOINT = "endpoint"
LABEL_STATUS = "status"

# Histogram bucket configurations
DELAY_BUCKETS = (
    60.0,      # 1 minute
    300.0,     # 5 minutes
    600.0,     # 10 minutes
    1800.0,    # 30 minutes
    3600.0,    # 1 hour
    7200.0,    # 2 hours
    14400.0,   # 4 hours
    28800.0,   # 8 hours
    43200.0,   # 12 hours
    86400.0,   # 24 hours
    float("inf"),
)

LATENCY_BUCKETS = (
    0.01,   # 10ms
    0.025,  # 25ms
    0.05,   # 50ms
    0.1,    # 100ms
    0.25,   # 250ms
    0.5,    # 500ms
    1.0,    # 1s
    2.5,    # 2.5s
    5.0,    # 5s
    10.0,   # 10s
    float("inf"),
)

# --- Core Metrics (from F-07 spec) ---

JOBS_HELD = Gauge(
    "carbon_jobs_held",
    "Number of jobs currently held/waiting for a green window",
    [LABEL_NAMESPACE],
)

JOB_DELAY_SECONDS = Histogram(
    "carbon_job_delay_seconds",
    "Distribution of job delays in seconds (time from submission to release)",
    [LABEL_NAMESPACE, LABEL_JOB_NAME],
    buckets=DELAY_BUCKETS,
)

CO2_AVOIDED_GRAMS = Counter(
    "carbon_co2_avoided_grams_total",
    "Cumulative estimated CO2 avoided in grams",
    [LABEL_NAMESPACE, LABEL_JOB_NAME],
)

GRID_RENEWABLE_SHARE = Gauge(
    "carbon_grid_renewable_share_percent",
    "Current renewable share percentage of the grid",
)

FALLBACK_RELEASES = Counter(
    "fallback_releases_total",
    "Number of fallback releases due to missing or degraded grid data",
    [LABEL_NAMESPACE, LABEL_JOB_NAME],
)

# --- Additional Metrics ---

JOBS_RELEASED = Counter(
    "carbon_jobs_released_total",
    "Total number of jobs released by the scheduler",
    [LABEL_NAMESPACE, LABEL_JOB_NAME],
)

API_REQUESTS = Counter(
    "carbon_api_requests_total",
    "Total API requests to external services",
    [LABEL_ENDPOINT, LABEL_STATUS],
)

API_LATENCY_SECONDS = Histogram(
    "carbon_api_latency_seconds",
    "Latency of API calls to external services in seconds",
    [LABEL_ENDPOINT],
    buckets=LATENCY_BUCKETS,
)

FORECAST_AGE_SECONDS = Gauge(
    "carbon_forecast_age_seconds",
    "Age of the current forecast data in seconds",
)


def setup_metrics(
    port: int = DEFAULT_METRICS_PORT,
    registry: CollectorRegistry | None = None,
) -> None:
    """Start the Prometheus HTTP server for metrics scraping.

    Args:
        port: Port to expose metrics on. Defaults to 8000.
        registry: Optional custom registry. Uses default REGISTRY if not provided.
    """
    effective_registry = registry if registry is not None else REGISTRY
    start_http_server(port, registry=effective_registry)
    logger.info("Prometheus metrics server started on port %d", port)


# --- Helper Functions ---


def record_job_held(namespace: str) -> None:
    """Increment the count of held jobs for a namespace.

    Args:
        namespace: Kubernetes namespace of the job.
    """
    JOBS_HELD.labels(namespace=namespace).inc()


def record_job_released(
    namespace: str,
    job_name: str,
    delay_seconds: float | None = None,
) -> None:
    """Record a job release, optionally with delay.

    Args:
        namespace: Kubernetes namespace of the job.
        job_name: Name of the job.
        delay_seconds: Optional delay in seconds from submission to release.
    """
    JOBS_HELD.labels(namespace=namespace).dec()
    JOBS_RELEASED.labels(namespace=namespace, job_name=job_name).inc()
    if delay_seconds is not None:
        JOB_DELAY_SECONDS.labels(namespace=namespace, job_name=job_name).observe(
            delay_seconds
        )


def record_fallback_release(namespace: str, job_name: str) -> None:
    """Record a fallback release due to missing grid data.

    Args:
        namespace: Kubernetes namespace of the job.
        job_name: Name of the job.
    """
    FALLBACK_RELEASES.labels(namespace=namespace, job_name=job_name).inc()


def record_co2_avoided(namespace: str, job_name: str, grams: float) -> None:
    """Record estimated CO2 avoided for a job.

    Args:
        namespace: Kubernetes namespace of the job.
        job_name: Name of the job.
        grams: Estimated CO2 avoided in grams.
    """
    CO2_AVOIDED_GRAMS.labels(namespace=namespace, job_name=job_name).inc(grams)


def update_renewable_share(percent: float) -> None:
    """Update the current grid renewable share.

    Args:
        percent: Renewable share as a percentage (0-100).
    """
    GRID_RENEWABLE_SHARE.set(percent)


def record_api_request(
    endpoint: str,
    status: str,
    latency_seconds: float | None = None,
) -> None:
    """Record an API request to an external service.

    Args:
        endpoint: Name/path of the API endpoint (e.g., 'ren_share_forecast', 'signal').
        status: Status of the request ('success', 'error', 'timeout', etc.).
        latency_seconds: Optional request latency in seconds.
    """
    API_REQUESTS.labels(endpoint=endpoint, status=status).inc()
    if latency_seconds is not None:
        API_LATENCY_SECONDS.labels(endpoint=endpoint).observe(latency_seconds)


def update_forecast_age(seconds: float) -> None:
    """Update the age of the current forecast data.

    Args:
        seconds: Age of the forecast in seconds.
    """
    FORECAST_AGE_SECONDS.set(seconds)


def get_jobs_held_count(namespace: str) -> float:
    """Get the current count of held jobs for a namespace.

    Args:
        namespace: Kubernetes namespace.

    Returns:
        Current count of held jobs.
    """
    return JOBS_HELD.labels(namespace=namespace)._value.get()


def reset_jobs_held(namespace: str) -> None:
    """Reset the held jobs count for a namespace to zero.

    Useful for controller restarts when rebuilding state.

    Args:
        namespace: Kubernetes namespace.
    """
    JOBS_HELD.labels(namespace=namespace).set(0)
