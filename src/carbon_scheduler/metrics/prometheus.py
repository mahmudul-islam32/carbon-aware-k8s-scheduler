"""Prometheus metrics for the Carbon-Aware Kubernetes Batch Scheduler.

This module defines all Prometheus metrics exposed by the scheduler and provides
helper functions for updating them consistently.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from prometheus_client import (
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    make_wsgi_app,
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

CO2_PLANNED_AVOIDED_GRAMS = Histogram(
    "carbon_job_planned_co2_avoided_grams",
    "Estimated CO2 avoided per held job at planning time (grams, ESTIMATE)",
    [LABEL_NAMESPACE],
    buckets=(0.0, 1.0, 5.0, 10.0, 50.0, 100.0, 500.0, 1000.0, 5000.0, float("inf")),
)

# Readiness flag served at /readyz; set once the controller has initialised.
_ready = threading.Event()

# Held jobs per namespace, rebuilt from Job annotations by the release timer so the
# carbon_jobs_held gauge stays correct across controller restarts.
_held_jobs: dict[str, set[str]] = {}
_held_lock = threading.Lock()


class _QuietHandler(WSGIRequestHandler):
    """Request handler that does not log every scrape."""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ARG002
        return


class _ThreadingWSGIServer(WSGIServer):
    """WSGI server that handles each request in a daemon thread."""

    daemon_threads = True

    def process_request(self, request: Any, client_address: Any) -> None:
        thread = threading.Thread(
            target=self._process_request_thread,
            args=(request, client_address),
            daemon=True,
        )
        thread.start()

    def _process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


def set_ready(ready: bool = True) -> None:
    """Mark the controller as ready (or not) for the /readyz probe."""
    if ready:
        _ready.set()
    else:
        _ready.clear()


def is_ready() -> bool:
    """Whether the controller reported itself ready."""
    return _ready.is_set()


def make_app(registry: CollectorRegistry | None = None) -> Any:
    """Build a WSGI app serving /metrics, /healthz and /readyz."""
    metrics_app = make_wsgi_app(registry if registry is not None else REGISTRY)

    def app(environ: dict[str, Any], start_response: Any) -> Any:
        path = environ.get("PATH_INFO", "/")
        if path == "/healthz":
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"ok"]
        if path == "/readyz":
            if is_ready():
                start_response("200 OK", [("Content-Type", "text/plain")])
                return [b"ready"]
            start_response("503 Service Unavailable", [("Content-Type", "text/plain")])
            return [b"not ready"]
        return metrics_app(environ, start_response)

    return app


def setup_metrics(
    port: int = DEFAULT_METRICS_PORT,
    registry: CollectorRegistry | None = None,
) -> None:
    """Start the HTTP server for metrics scraping and health probes.

    Serves ``/metrics`` (and every other path) with Prometheus metrics, plus
    ``/healthz`` (liveness) and ``/readyz`` (readiness).

    Args:
        port: Port to expose metrics on. Defaults to 8000.
        registry: Optional custom registry. Uses default REGISTRY if not provided.
    """
    start_metrics_server(port, registry=registry)


def start_metrics_server(
    port: int = DEFAULT_METRICS_PORT,
    addr: str = "0.0.0.0",
    registry: CollectorRegistry | None = None,
) -> WSGIServer:
    """Start the metrics/health server in a daemon thread and return it."""
    server = make_server(
        addr,
        port,
        make_app(registry),
        server_class=_ThreadingWSGIServer,
        handler_class=_QuietHandler,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Metrics and health server started on port %d", server.server_port)
    return server


# --- Helper Functions ---


def record_job_held(namespace: str) -> None:
    """Increment the count of held jobs for a namespace.

    Args:
        namespace: Kubernetes namespace of the job.
    """
    JOBS_HELD.labels(namespace=namespace).inc()


def track_held_job(namespace: str, job_name: str) -> None:
    """Register a job as held. Idempotent, so it is safe to call on every reconcile.

    Args:
        namespace: Kubernetes namespace of the job.
        job_name: Name of the job.
    """
    with _held_lock:
        jobs = _held_jobs.setdefault(namespace, set())
        jobs.add(job_name)
        JOBS_HELD.labels(namespace=namespace).set(len(jobs))


def untrack_held_job(namespace: str, job_name: str) -> bool:
    """Remove a job from the held set.

    Args:
        namespace: Kubernetes namespace of the job.
        job_name: Name of the job.

    Returns:
        True if the job was tracked as held.
    """
    with _held_lock:
        jobs = _held_jobs.setdefault(namespace, set())
        was_held = job_name in jobs
        jobs.discard(job_name)
        JOBS_HELD.labels(namespace=namespace).set(len(jobs))
        return was_held


def record_planned_co2_avoided(namespace: str, grams: float) -> None:
    """Observe the estimated CO2 avoided for a newly planned job.

    Args:
        namespace: Kubernetes namespace of the job.
        grams: Estimated CO2 avoided in grams.
    """
    CO2_PLANNED_AVOIDED_GRAMS.labels(namespace=namespace).observe(max(0.0, grams))


def record_job_released(
    namespace: str,
    job_name: str,
    delay_seconds: float | None = None,
    adjust_held: bool = True,
) -> None:
    """Record a job release, optionally with delay.

    Args:
        namespace: Kubernetes namespace of the job.
        job_name: Name of the job.
        delay_seconds: Optional delay in seconds from submission to release.
        adjust_held: Decrement carbon_jobs_held. Pass False when the held gauge is
            managed with track_held_job/untrack_held_job.
    """
    if adjust_held:
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
    value: float = JOBS_HELD.labels(namespace=namespace)._value.get()
    return value


def reset_jobs_held(namespace: str) -> None:
    """Reset the held jobs count for a namespace to zero.

    Useful for controller restarts when rebuilding state.

    Args:
        namespace: Kubernetes namespace.
    """
    with _held_lock:
        _held_jobs.pop(namespace, None)
    JOBS_HELD.labels(namespace=namespace).set(0)
