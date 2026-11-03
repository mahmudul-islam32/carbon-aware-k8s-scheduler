"""Thread-safe in-memory state behind the web dashboard.

The controller (or the demo simulator) pushes the latest forecast, job
decisions and Events into a :class:`DashboardState`; the HTTP server serves
:meth:`DashboardState.snapshot` as JSON at ``/api/state``.

The state is a view, not a source of truth: after a restart the release timer
re-registers held Jobs from their annotations, so nothing is lost that matters.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from carbon_scheduler.planner.models import Forecast

JobStatus = Literal["held", "released", "fallback"]

MAX_EVENTS = 200
MAX_JOBS = 500
MAX_CO2_POINTS = 2000
#: How far ahead "peak ahead" looks (matches the dashboard's visible range).
PEAK_HORIZON = timedelta(hours=36)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


@dataclass
class JobRecord:
    """One Job as the dashboard shows it."""

    namespace: str
    name: str
    status: JobStatus
    policy: str | None = None
    submitted: datetime | None = None
    planned_start: datetime | None = None
    latest_start: datetime | None = None
    deadline: datetime | None = None
    runtime_seconds: float | None = None
    expected_share_pct: float | None = None
    baseline_share_pct: float | None = None
    co2_avoided_g: float | None = None
    released_at: datetime | None = None
    fallback_reason: str | None = None
    team: str | None = None

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.name}"

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        for k, v in data.items():
            if isinstance(v, datetime):
                data[k] = v.isoformat()
        data["key"] = self.key
        return data


@dataclass
class _Event:
    time: datetime
    namespace: str
    name: str
    reason: str
    message: str
    type: str = "Normal"

    def to_json(self) -> dict[str, Any]:
        return {
            "time": self.time.isoformat(),
            "namespace": self.namespace,
            "name": self.name,
            "reason": self.reason,
            "message": self.message,
            "type": self.type,
        }


@dataclass
class DashboardState:
    """Everything the dashboard renders, guarded by one lock."""

    mode: str = "live"
    country: str = "de"
    policy: str = "greenest-window"
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    _forecast: list[tuple[datetime, float]] = field(default_factory=list)
    _forecast_fetched_at: datetime | None = None
    _jobs: dict[str, JobRecord] = field(default_factory=dict)
    _events: deque[_Event] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    _co2_series: list[tuple[datetime, float]] = field(default_factory=list)
    _co2_total_g: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ---- writers -------------------------------------------------------

    def configure(self, *, mode: str | None = None, country: str | None = None,
                  policy: str | None = None) -> None:
        """Set header metadata."""
        with self._lock:
            if mode is not None:
                self.mode = mode
            if country is not None:
                self.country = country
            if policy is not None:
                self.policy = policy

    def set_forecast(self, forecast: Forecast | None, fetched_at: datetime | None = None) -> None:
        """Replace the forecast curve."""
        if forecast is None:
            return
        with self._lock:
            self._forecast = [(p.timestamp, p.renewable_share_pct) for p in forecast.points]
            self._forecast_fetched_at = fetched_at or self.clock()

    def upsert_job(self, record: JobRecord) -> None:
        """Insert or update a Job, keeping fields the new record leaves empty."""
        with self._lock:
            existing = self._jobs.get(record.key)
            if existing is not None:
                for k, v in asdict(record).items():
                    if v is not None:
                        setattr(existing, k, v)
            else:
                if len(self._jobs) >= MAX_JOBS:
                    self._evict_oldest_finished()
                self._jobs[record.key] = record

    def mark_released(
        self,
        namespace: str,
        name: str,
        *,
        fallback: bool = False,
        reason: str | None = None,
        at: datetime | None = None,
        co2_avoided_g: float | None = None,
    ) -> None:
        """Record a release; avoided CO2 is added to the running total."""
        when = at or self.clock()
        key = f"{namespace}/{name}"
        with self._lock:
            job = self._jobs.get(key)
            if job is None:
                job = JobRecord(namespace=namespace, name=name, status="released")
                self._jobs[key] = job
            job.status = "fallback" if fallback else "released"
            job.released_at = when
            if reason:
                job.fallback_reason = reason
            if co2_avoided_g is not None:
                job.co2_avoided_g = co2_avoided_g
            gained = job.co2_avoided_g if (job.co2_avoided_g and not fallback) else 0.0
            if gained > 0:
                self._co2_total_g += gained
                self._co2_series.append((when, self._co2_total_g))
                if len(self._co2_series) > MAX_CO2_POINTS:
                    self._co2_series = self._co2_series[-MAX_CO2_POINTS:]

    def add_event(
        self, namespace: str, name: str, reason: str, message: str, event_type: str = "Normal"
    ) -> None:
        """Append a Kubernetes-style Event to the feed."""
        with self._lock:
            self._events.appendleft(
                _Event(self.clock(), namespace, name, reason, message, event_type)
            )

    def reset(self) -> None:
        """Forget everything (tests, demo restart)."""
        with self._lock:
            self._forecast.clear()
            self._forecast_fetched_at = None
            self._jobs.clear()
            self._events.clear()
            self._co2_series.clear()
            self._co2_total_g = 0.0

    def _evict_oldest_finished(self) -> None:
        finished = [j for j in self._jobs.values() if j.status != "held"]
        if not finished:
            return
        oldest = min(finished, key=lambda j: j.released_at or datetime.min.replace(tzinfo=UTC))
        self._jobs.pop(oldest.key, None)

    # ---- readers -------------------------------------------------------

    def current_share(self, now: datetime | None = None) -> float | None:
        """Forecast share at (or just before) ``now``."""
        now = now or self.clock()
        with self._lock:
            best: float | None = None
            for ts, share in self._forecast:
                if ts <= now:
                    best = share
                else:
                    break
            if best is None and self._forecast:
                best = self._forecast[0][1]
            return best

    def snapshot(self) -> dict[str, Any]:
        """JSON-serialisable view for ``/api/state``."""
        now = self.clock()
        share_now = self.current_share(now)
        with self._lock:
            jobs = sorted(
                self._jobs.values(),
                key=lambda j: j.planned_start or j.submitted or now,
            )
            held = [j for j in jobs if j.status == "held"]
            released = [j for j in jobs if j.status != "held"]
            delays = [
                (j.released_at - j.submitted).total_seconds()
                for j in released
                if j.released_at and j.submitted
            ]
            planned_avoided = sum(j.co2_avoided_g or 0.0 for j in held)
            horizon = now + PEAK_HORIZON
            future = [s for ts, s in self._forecast if now <= ts <= horizon]
            return {
                "now": now.isoformat(),
                "mode": self.mode,
                "country": self.country,
                "policy": self.policy,
                "forecast_fetched_at": _iso(self._forecast_fetched_at),
                "forecast": [[ts.isoformat(), share] for ts, share in self._forecast],
                "jobs": [j.to_json() for j in jobs],
                "events": [e.to_json() for e in self._events],
                "co2_series": [[ts.isoformat(), g] for ts, g in self._co2_series],
                "totals": {
                    "held": len(held),
                    "released": sum(1 for j in released if j.status == "released"),
                    "fallback": sum(1 for j in released if j.status == "fallback"),
                    "co2_avoided_g": round(self._co2_total_g, 2),
                    "co2_planned_g": round(planned_avoided, 2),
                    "mean_delay_seconds": (sum(delays) / len(delays)) if delays else None,
                    "share_now": share_now,
                    "share_peak_ahead": max(future) if future else None,
                },
            }


#: Process-wide state used by the controller and the metrics server.
STATE = DashboardState()
