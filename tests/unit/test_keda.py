"""Tests for KEDA ScaledJob mode (F-11)."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from prometheus_client import CollectorRegistry

from carbon_scheduler.config import Settings
from carbon_scheduler.controller import handlers
from carbon_scheduler.controller.scheduler import JobScheduler
from carbon_scheduler.grid.models import GridData, RenewableShareForecast
from carbon_scheduler.keda import KEDA_PATH, ConcurrencyBand, GreennessSource, refresh_once
from carbon_scheduler.keda import scaler as keda_scaler
from carbon_scheduler.metrics.prometheus import KEDA_ALLOWED_CONCURRENCY, make_app
from carbon_scheduler.planner.models import Forecast, ForecastPoint

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def _forecast(shares: list[float], start: datetime = NOW - timedelta(minutes=30)) -> Forecast:
    return Forecast(
        points=[
            ForecastPoint(timestamp=start + timedelta(minutes=15 * i), renewable_share_pct=s)
            for i, s in enumerate(shares)
        ]
    )


def _source(share: float | None = None, **kwargs: Any) -> GreennessSource:
    source = GreennessSource(clock=lambda: NOW, **kwargs)
    if share is not None:
        source.update(_forecast([share] * 8), fetched_at=NOW - timedelta(minutes=5), degraded=False)
    return source


def _call(app: Any, path: str, query: str = "") -> tuple[str, dict[str, Any]]:
    captured: dict[str, str] = {}

    def start_response(status: str, _headers: list[tuple[str, str]]) -> None:
        captured["status"] = status

    body = b"".join(app({"PATH_INFO": path, "QUERY_STRING": query, "REQUEST_METHOD": "GET"},
                        start_response))
    return captured["status"], json.loads(body)


class TestConcurrencyBand:
    band = ConcurrencyBand(min_concurrency=0, max_concurrency=10,
                           low_share_pct=40, high_share_pct=80)

    @pytest.mark.parametrize(
        ("share", "expected"),
        [(0, 0), (40, 0), (50, 2), (60, 5), (79.9, 9), (80, 10), (100, 10)],
    )
    def test_linear_between_low_and_high(self, share: float, expected: int) -> None:
        assert self.band.allowed(share) == expected

    def test_missing_share_fails_open_to_max(self) -> None:
        assert self.band.allowed(None) == 10

    def test_levels(self) -> None:
        assert [self.band.level(s) for s in (30, 60, 90)] == ["red", "partial", "green"]

    def test_min_floor_is_respected(self) -> None:
        band = ConcurrencyBand(min_concurrency=2, max_concurrency=6)
        assert band.allowed(0) == 2
        assert band.allowed(100) == 6

    @pytest.mark.parametrize(
        "kwargs",
        [{"min_concurrency": 5, "max_concurrency": 2}, {"low_share_pct": 80, "high_share_pct": 80}],
    )
    def test_invalid_band_is_rejected(self, kwargs: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            ConcurrencyBand(**kwargs)

    def test_from_settings(self) -> None:
        settings = Settings(keda_min_concurrency=1, keda_max_concurrency=4,
                            keda_low_share_pct=30, keda_high_share_pct=70)
        assert ConcurrencyBand.from_settings(settings) == ConcurrencyBand(1, 4, 30, 70)


class TestSettings:
    def test_defaults_keep_keda_off(self) -> None:
        settings = Settings()
        assert settings.keda_enabled is False
        assert (settings.keda_min_concurrency, settings.keda_max_concurrency) == (0, 10)

    def test_env_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CARBON_KEDA_ENABLED", "true")
        monkeypatch.setenv("CARBON_KEDA_MAX_CONCURRENCY", "25")
        settings = Settings()
        assert settings.keda_enabled is True
        assert settings.keda_max_concurrency == 25

    @pytest.mark.parametrize(
        "kwargs",
        [{"keda_min_concurrency": 5, "keda_max_concurrency": 1},
         {"keda_low_share_pct": 90, "keda_high_share_pct": 50}],
    )
    def test_inverted_ranges_fail_at_startup(self, kwargs: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            Settings(**kwargs)


class TestGreennessSource:
    def test_reports_allowed_concurrency_from_current_share(self) -> None:
        payload = _source(60).evaluate()
        assert payload["allowedConcurrency"] == 5
        assert payload["renewableSharePct"] == 60
        assert payload["level"] == "partial"
        assert payload["fallbackReason"] is None
        assert payload["forecastAgeSeconds"] == 300

    def test_uses_the_point_at_or_before_now(self) -> None:
        source = GreennessSource(clock=lambda: NOW)
        # Points at -30m, -15m, 0, +15m: the one at NOW (85 %) is current.
        source.update(_forecast([10, 20, 85, 5]), fetched_at=NOW, degraded=False)
        assert source.evaluate()["renewableSharePct"] == 85

    @pytest.mark.parametrize(
        ("setup", "reason"),
        [
            ("empty", "no_forecast_data"),
            ("degraded", "forecast_data_degraded"),
            ("stale", "forecast_stale"),
            ("future_only", "no_forecast_for_now"),
            ("expired", "no_forecast_for_now"),
        ],
    )
    def test_fails_open_to_max(self, setup: str, reason: str) -> None:
        source = GreennessSource(clock=lambda: NOW, max_age=timedelta(hours=1))
        fresh = NOW - timedelta(minutes=1)
        if setup == "degraded":
            source.update(_forecast([10] * 8), fetched_at=fresh, degraded=True)
        elif setup == "stale":
            source.update(_forecast([10] * 8), fetched_at=NOW - timedelta(hours=2), degraded=False)
        elif setup == "future_only":
            source.update(_forecast([10] * 4, start=NOW + timedelta(hours=1)), fresh, False)
        elif setup == "expired":
            source.update(_forecast([10] * 4, start=NOW - timedelta(hours=5)), fresh, False)
        payload = source.evaluate()
        assert payload["allowedConcurrency"] == 10
        assert payload["level"] == "fallback"
        assert payload["fallbackReason"] == reason

    def test_naive_fetched_at_is_accepted(self) -> None:
        source = GreennessSource(clock=lambda: datetime.now(UTC))
        source.update(_forecast([90] * 8, start=datetime.now(UTC) - timedelta(minutes=30)),
                      fetched_at=datetime.now(), degraded=False)
        assert source.evaluate()["allowedConcurrency"] == 10


class TestEndpoint:
    @pytest.fixture
    def app(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        monkeypatch.setattr(keda_scaler, "SOURCE", _source(60))
        return make_app(CollectorRegistry(), dashboard=False, keda=True)

    def test_serves_metrics_api_payload(self, app: Any) -> None:
        status, payload = _call(app, KEDA_PATH)
        assert status == "200 OK"
        assert payload["allowedConcurrency"] == 5
        assert KEDA_ALLOWED_CONCURRENCY._value.get() == 5

    def test_query_overrides_band(self, app: Any) -> None:
        _, payload = _call(app, KEDA_PATH, "max=20")
        assert payload["maxConcurrency"] == 20
        assert payload["allowedConcurrency"] == 10
        _, payload = _call(app, KEDA_PATH, "min=1&max=3")
        assert payload["allowedConcurrency"] == 2

    @pytest.mark.parametrize("query", ["max=-1", "max=abc", "min=9&max=3"])
    def test_bad_query_is_a_400(self, app: Any, query: str) -> None:
        status, payload = _call(app, KEDA_PATH, query)
        assert status == "400 Bad Request"
        assert "error" in payload

    def test_not_served_when_disabled(self) -> None:
        app = make_app(CollectorRegistry(), dashboard=False, keda=False)
        captured: dict[str, str] = {}
        body = b"".join(app({"PATH_INFO": KEDA_PATH, "REQUEST_METHOD": "GET"},
                            lambda status, _h: captured.setdefault("status", status)))
        assert b"allowedConcurrency" not in body


class TestRefresher:
    def _grid(self, substitute: bool = False) -> GridData:
        start = int((NOW - timedelta(minutes=30)).timestamp())
        return GridData(
            forecast=RenewableShareForecast(
                unix_seconds=[start + 900 * i for i in range(8)],
                ren_share=[70.0] * 8,
                substitute=substitute,
            ),
            fetched_at=NOW,
            country="de",
        )

    async def test_refresh_once_feeds_the_source(self) -> None:
        client = MagicMock()
        client.get_grid_data = AsyncMock(return_value=self._grid())
        scheduler = JobScheduler(grid_client=client, settings=Settings())
        source = GreennessSource(clock=lambda: NOW)
        assert await refresh_once(scheduler, source) is True
        assert source.evaluate()["allowedConcurrency"] == 7

    async def test_refresh_without_data_keeps_failing_open(self) -> None:
        scheduler = JobScheduler(grid_client=None, settings=Settings())
        source = GreennessSource(clock=lambda: NOW)
        assert await refresh_once(scheduler, source) is False
        assert source.evaluate()["fallbackReason"] == "no_forecast_data"

    async def test_refresher_starts_only_when_enabled(self) -> None:
        scheduler = JobScheduler(grid_client=None, settings=Settings())
        assert handlers.start_keda_refresher(scheduler, Settings()) is None
        task = handlers.start_keda_refresher(scheduler, Settings(keda_enabled=True))
        assert task is not None
        await asyncio.sleep(0)
        assert not task.done()
        await handlers.stop_keda_refresher()
        assert task.cancelled()


class TestPackaging:
    def test_helm_values_and_env(self) -> None:
        values = yaml.safe_load((ROOT / "charts/carbon-scheduler/values.yaml").read_text())
        assert values["keda"] == {
            "enabled": False, "minConcurrency": 0, "maxConcurrency": 10,
            "lowSharePct": 40, "highSharePct": 80,
        }
        deployment = (ROOT / "charts/carbon-scheduler/templates/deployment.yaml").read_text()
        for env in ("ENABLED", "MIN_CONCURRENCY", "MAX_CONCURRENCY", "LOW_SHARE_PCT",
                    "HIGH_SHARE_PCT"):
            assert f"CARBON_KEDA_{env}" in deployment

    def test_example_scaledjob_uses_the_endpoint(self) -> None:
        doc = yaml.safe_load((ROOT / "examples/keda-scaledjob.yaml").read_text())
        assert doc["kind"] == "ScaledJob"
        assert doc["spec"]["scalingStrategy"]["multipleScalersCalculation"] == "min"
        carbon = next(t for t in doc["spec"]["triggers"] if t["type"] == "metrics-api")
        assert KEDA_PATH in carbon["metadata"]["url"]
        assert carbon["metadata"]["valueLocation"] == "allowedConcurrency"
