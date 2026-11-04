"""KEDA ScaledJob mode (F-11): grid greenness as "allowed concurrency".

Teams that already run queue workers with a KEDA ``ScaledJob`` don't want to
annotate every Job. Instead, KEDA's ``metrics-api`` scaler polls
``/keda/allowed-concurrency`` on the controller's metrics port and gets back how
many Jobs may run *right now*:

* renewable share at or below ``low_share_pct``  -> ``min_concurrency``
* renewable share at or above ``high_share_pct`` -> ``max_concurrency``
* in between                                     -> linear, rounded down
* no data, degraded (``substitute``) or stale data -> ``max_concurrency``
  (fail-open: run as if the scheduler were not there)

Combined with a queue trigger and ``scalingStrategy.multipleScalersCalculation:
min``, KEDA starts ``min(queue length, allowed concurrency)`` Jobs, so work drains
fast when the grid is green and trickles when it is not.
"""

from __future__ import annotations

import asyncio
import json
import math
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

import structlog

from carbon_scheduler.planner.models import Forecast

if TYPE_CHECKING:
    from carbon_scheduler.config import Settings
    from carbon_scheduler.controller.scheduler import JobScheduler

logger = structlog.get_logger(__name__)

KEDA_PATH = "/keda/allowed-concurrency"


@dataclass(frozen=True)
class ConcurrencyBand:
    """How renewable share maps to allowed concurrency.

    Attributes:
        min_concurrency: Jobs allowed at or below ``low_share_pct``.
        max_concurrency: Jobs allowed at or above ``high_share_pct`` and on fail-open.
        low_share_pct: Renewable share (%) treated as "dirty".
        high_share_pct: Renewable share (%) treated as "green".
    """

    min_concurrency: int = 0
    max_concurrency: int = 10
    low_share_pct: float = 40.0
    high_share_pct: float = 80.0

    def __post_init__(self) -> None:
        if self.min_concurrency < 0 or self.min_concurrency > self.max_concurrency:
            raise ValueError("need 0 <= min_concurrency <= max_concurrency")
        if self.low_share_pct >= self.high_share_pct:
            raise ValueError("low_share_pct must be < high_share_pct")

    @classmethod
    def from_settings(cls, settings: Settings) -> ConcurrencyBand:
        """Build the band from ``CARBON_KEDA_*`` settings."""
        return cls(
            min_concurrency=settings.keda_min_concurrency,
            max_concurrency=settings.keda_max_concurrency,
            low_share_pct=settings.keda_low_share_pct,
            high_share_pct=settings.keda_high_share_pct,
        )

    def allowed(self, share_pct: float | None) -> int:
        """Allowed concurrency for a renewable share; ``None`` fails open to the maximum."""
        if share_pct is None:
            return self.max_concurrency
        if share_pct <= self.low_share_pct:
            return self.min_concurrency
        if share_pct >= self.high_share_pct:
            return self.max_concurrency
        fraction = (share_pct - self.low_share_pct) / (self.high_share_pct - self.low_share_pct)
        span = self.max_concurrency - self.min_concurrency
        return self.min_concurrency + math.floor(fraction * span)

    def level(self, share_pct: float) -> str:
        """``green``, ``partial`` or ``red`` for a renewable share."""
        if share_pct >= self.high_share_pct:
            return "green"
        if share_pct <= self.low_share_pct:
            return "red"
        return "partial"


@dataclass
class _Snapshot:
    forecast: Forecast | None = None
    fetched_at: datetime | None = None
    degraded: bool = False


class GreennessSource:
    """Thread-safe holder of the latest forecast, read by the KEDA endpoint.

    The controller's refresher (:func:`refresh_loop`) writes it; the WSGI handler
    on the metrics server thread reads it.
    """

    def __init__(
        self,
        band: ConcurrencyBand | None = None,
        max_age: timedelta = timedelta(hours=1),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Create an empty source.

        Args:
            band: Share-to-concurrency mapping.
            max_age: Forecasts older than this are treated as stale (fail-open).
            clock: Time source (overridable in tests).
        """
        self.band = band or ConcurrencyBand()
        self.max_age = max_age
        self.clock = clock
        self._snapshot = _Snapshot()
        self._lock = threading.Lock()

    def configure(self, band: ConcurrencyBand, max_age: timedelta) -> None:
        """Apply settings at controller start-up."""
        with self._lock:
            self.band = band
            self.max_age = max_age

    def update(self, forecast: Forecast | None, fetched_at: datetime, degraded: bool) -> None:
        """Store a freshly fetched forecast."""
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.astimezone(UTC)
        with self._lock:
            self._snapshot = _Snapshot(forecast=forecast, fetched_at=fetched_at, degraded=degraded)

    def evaluate(self, band: ConcurrencyBand | None = None) -> dict[str, Any]:
        """Compute the KEDA payload for now.

        Args:
            band: Band to use instead of the configured one (query overrides).

        Returns:
            JSON-serialisable dict; ``allowedConcurrency`` is what KEDA reads.
        """
        now = self.clock()
        with self._lock:
            snap = self._snapshot
            band = band or self.band
            max_age = self.max_age

        share: float | None = None
        reason: str | None = None
        age: float | None = None
        if snap.fetched_at is not None:
            age = max(0.0, (now - snap.fetched_at).total_seconds())

        if snap.forecast is None or not snap.forecast.points:
            reason = "no_forecast_data"
        elif snap.degraded:
            reason = "forecast_data_degraded"
        elif age is not None and age > max_age.total_seconds():
            reason = "forecast_stale"
        else:
            share = _share_at(snap.forecast, now)
            if share is None:
                reason = "no_forecast_for_now"

        allowed = band.allowed(share)
        return {
            "allowedConcurrency": allowed,
            "renewableSharePct": share,
            "level": band.level(share) if share is not None else "fallback",
            "fallbackReason": reason,
            "minConcurrency": band.min_concurrency,
            "maxConcurrency": band.max_concurrency,
            "lowSharePct": band.low_share_pct,
            "highSharePct": band.high_share_pct,
            "forecastAgeSeconds": age,
            "now": now.isoformat(),
        }


def _share_at(forecast: Forecast, now: datetime) -> float | None:
    """Forecast share of the last point at or before ``now``, if the forecast covers ``now``."""
    points = forecast.points
    if not points or now < points[0].timestamp:
        return None
    best: float | None = None
    for point in points:
        if point.timestamp <= now:
            best = point.renewable_share_pct
        else:
            return best
    # ``now`` lies after the last point: only trust it within one forecast step.
    step = points[-1].timestamp - points[-2].timestamp if len(points) > 1 else timedelta(hours=1)
    return best if now - points[-1].timestamp <= step else None


#: Process-wide source used by the controller and the metrics server.
SOURCE = GreennessSource()


def _band_with_overrides(band: ConcurrencyBand, query: str) -> ConcurrencyBand:
    """Apply ``?min=&max=`` query overrides (one ScaledJob may want a different cap)."""
    params = parse_qs(query)
    values: dict[str, int] = {}
    for key, field_name in (("min", "min_concurrency"), ("max", "max_concurrency")):
        if key in params:
            raw = params[key][-1]
            if not raw.isdigit():
                raise ValueError(f"'{key}' must be a non-negative integer, got {raw!r}")
            values[field_name] = int(raw)
    if not values:
        return band
    # ConcurrencyBand rejects min > max, which surfaces as a 400.
    return ConcurrencyBand(
        min_concurrency=values.get("min_concurrency", band.min_concurrency),
        max_concurrency=values.get("max_concurrency", band.max_concurrency),
        low_share_pct=band.low_share_pct,
        high_share_pct=band.high_share_pct,
    )


def handle(
    environ: dict[str, Any],
    start_response: Callable[..., Any],
    source: GreennessSource | None = None,
) -> Iterable[bytes] | None:
    """Serve ``/keda/allowed-concurrency``, or return None if the path is not ours."""
    if environ.get("PATH_INFO", "/") != KEDA_PATH:
        return None
    source = source or SOURCE
    headers = [("Content-Type", "application/json"), ("Cache-Control", "no-store")]
    try:
        band = _band_with_overrides(source.band, environ.get("QUERY_STRING", ""))
    except ValueError as e:
        start_response("400 Bad Request", headers)
        return [json.dumps({"error": str(e)}).encode()]
    payload = source.evaluate(band)
    from carbon_scheduler.metrics.prometheus import update_keda_allowed_concurrency

    update_keda_allowed_concurrency(payload["allowedConcurrency"])
    start_response("200 OK", headers)
    return [json.dumps(payload).encode()]


async def refresh_once(scheduler: JobScheduler, source: GreennessSource | None = None) -> bool:
    """Fetch grid data through the scheduler's cached client and update the source.

    Returns:
        True if a forecast was stored.
    """
    from carbon_scheduler.controller.scheduler import grid_data_to_forecast
    from carbon_scheduler.dashboard.state import STATE

    source = source or SOURCE
    grid_data = await scheduler.get_grid_data()
    if grid_data is None:
        return False
    forecast = grid_data_to_forecast(grid_data)
    source.update(forecast, grid_data.fetched_at, grid_data.is_degraded)
    STATE.set_forecast(forecast, fetched_at=grid_data.fetched_at)
    return forecast is not None


async def refresh_loop(
    scheduler: JobScheduler,
    interval_seconds: float,
    source: GreennessSource | None = None,
) -> None:
    """Keep the source fresh until cancelled (the client's cache bounds API calls)."""
    while True:
        try:
            await refresh_once(scheduler, source)
        except Exception as e:  # never let the loop die; the endpoint fails open
            logger.error("keda_refresh_failed", error=str(e))
        await asyncio.sleep(interval_seconds)


__all__ = [
    "KEDA_PATH",
    "SOURCE",
    "ConcurrencyBand",
    "GreennessSource",
    "handle",
    "refresh_loop",
    "refresh_once",
]
