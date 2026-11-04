"""Mock Energy-Charts API for end-to-end tests (F-33).

Serves the three endpoints the controller reads (``/ren_share_forecast``,
``/signal`` and ``/public_power``) with a deterministic "step" profile:

* the renewable share is ``low_share`` until the *green start*,
* and ``high_share`` from the green start onwards.

The green start is ``green_after`` seconds after the **first forecast request**,
so a controller that plans a Job right after it fetches the forecast always
sees a green window a fixed time in the future, however long the cluster took
to come up. Later requests reuse the same anchor, so cache refreshes and
re-planning never move the window.

Run it with ``carbon-sim mock-api`` (the E2E test deploys it in the cluster
with the controller image), or embed :class:`MockEnergyChartsServer` in tests.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class MockProfile:
    """Shape of the mocked renewable-share curve.

    Attributes:
        green_after: Seconds between the first forecast request and the green start.
        low_share: Renewable share (%) before the green start.
        high_share: Renewable share (%) from the green start onwards.
        step: Spacing of the data points (seconds).
        horizon: How far ahead the forecast reaches (seconds).
        history: How far back the data reaches (seconds).
        substitute: Value of the ``substitute`` flag (True simulates degraded data).
    """

    green_after: float = 90.0
    low_share: float = 20.0
    high_share: float = 90.0
    step: int = 60
    horizon: int = 24 * 3600
    history: int = 3600
    substitute: bool = False
    anchor: float | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def green_start(self, now: float) -> float:
        """Return the green start, anchoring it on the first call."""
        with self._lock:
            if self.anchor is None:
                self.anchor = now
                logger.info("mock_api_anchored", green_start=now + self.green_after)
            return self.anchor + self.green_after

    def timestamps(self, now: float) -> list[int]:
        """Data point timestamps aligned to ``step``, from ``now - history`` to ``now + horizon``."""
        first = int((now - self.history) // self.step * self.step)
        last = int(now + self.horizon)
        return list(range(first, last + 1, self.step))

    def share_at(self, ts: float, green_start: float) -> float:
        """Renewable share at ``ts`` for a given green start."""
        return self.high_share if ts >= green_start else self.low_share

    def forecast(self, now: float) -> dict[str, Any]:
        """Payload of ``/ren_share_forecast``."""
        green = self.green_start(now)
        stamps = self.timestamps(now)
        return {
            "unix_seconds": stamps,
            "ren_share": [self.share_at(ts, green) for ts in stamps],
            "substitute": self.substitute,
        }

    def signal(self, now: float) -> dict[str, Any]:
        """Payload of ``/signal`` (0 = red, 1 = yellow, 2 = green)."""
        green = self.green_start(now)
        stamps = self.timestamps(now)
        return {
            "unix_seconds": stamps,
            "signal": [2 if ts >= green else 0 for ts in stamps],
            "substitute": self.substitute,
        }

    def public_power(self, now: float) -> dict[str, Any]:
        """Payload of ``/public_power`` in the v1 key-per-production-type shape."""
        green = self.green_start(now)
        stamps = self.timestamps(now)
        total = 50_000.0
        renewable = [total * self.share_at(ts, green) / 100 for ts in stamps]
        return {
            "unix_seconds": stamps,
            "Solar": [r * 0.6 for r in renewable],
            "Wind onshore": [r * 0.4 for r in renewable],
            "Fossil gas": [total - r for r in renewable],
        }


ROUTES: dict[str, Callable[[MockProfile, float], dict[str, Any]]] = {
    "/ren_share_forecast": MockProfile.forecast,
    "/signal": MockProfile.signal,
    "/public_power": MockProfile.public_power,
}


def _handler_for(profile: MockProfile, clock: Callable[[], float]) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class bound to a profile and a clock."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/healthz":
                self._send(200, {"status": "ok"})
                return
            route = ROUTES.get(path)
            if route is None:
                self._send(404, {"error": f"unknown endpoint {path}"})
                return
            self._send(200, route(profile, clock()))
            logger.info("mock_api_request", path=path)

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            """Silence the default stderr access log (structlog covers it)."""

    return Handler


class MockEnergyChartsServer:
    """Threaded HTTP server that mimics the Energy-Charts endpoints."""

    def __init__(
        self,
        profile: MockProfile | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Bind the server (``port=0`` picks a free port).

        Args:
            profile: Curve to serve; defaults to :class:`MockProfile` defaults.
            host: Address to bind.
            port: Port to bind.
            clock: Time source in unix seconds (overridable in tests).
        """
        self.profile = profile or MockProfile()
        self._server = ThreadingHTTPServer((host, port), _handler_for(self.profile, clock))
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """Port the server listens on."""
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        """Base URL to point ``CARBON_GRID_API_BASE_URL`` at."""
        host = self._server.server_address[0]
        if isinstance(host, bytes):
            host = host.decode()
        return f"http://{host}:{self.port}"

    def start(self) -> MockEnergyChartsServer:
        """Serve in a background thread."""
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        """Serve in the calling thread until interrupted."""
        self._server.serve_forever()

    def stop(self) -> None:
        """Stop serving and release the socket."""
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> MockEnergyChartsServer:
        return self.start()

    def __exit__(self, *_args: object) -> None:
        self.stop()


__all__ = ["MockEnergyChartsServer", "MockProfile", "ROUTES"]
