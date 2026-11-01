"""WSGI routes for the dashboard UI and its JSON API."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from functools import lru_cache
from importlib import resources
from typing import Any

from carbon_scheduler.dashboard.state import STATE, DashboardState

DASHBOARD_PATHS = ("/", "/dashboard", "/dashboard/")


@lru_cache(maxsize=1)
def dashboard_html() -> bytes:
    """The single-page dashboard, packaged next to this module."""
    return resources.files("carbon_scheduler.dashboard").joinpath("static/index.html").read_bytes()


def handle(
    environ: dict[str, Any],
    start_response: Callable[..., Any],
    state: DashboardState | None = None,
) -> Iterable[bytes] | None:
    """Serve a dashboard route, or return None if the path is not ours."""
    path = environ.get("PATH_INFO", "/")
    if path in DASHBOARD_PATHS:
        start_response(
            "200 OK",
            [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-cache")],
        )
        return [dashboard_html()]
    if path == "/api/state":
        body = json.dumps((state or STATE).snapshot()).encode()
        start_response(
            "200 OK",
            [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-store"),
                ("Access-Control-Allow-Origin", "*"),
            ],
        )
        return [body]
    return None
