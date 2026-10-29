"""Tests for Energy-Charts grid client.

Uses pytest-httpx for mocking HTTP responses.
"""

import asyncio
import time
from datetime import datetime

import pytest
from pytest_httpx import HTTPXMock

from carbon_scheduler.grid.cache import CacheEntry, GridCache, RateLimitState
from carbon_scheduler.grid.client import APIError, EnergyChartsClient, RateLimitError
from carbon_scheduler.grid.models import (
    DataQuality,
    GridData,
    GridSignal,
    PublicPower,
    RenewableShareForecast,
)

# Sample API responses
SAMPLE_FORECAST_RESPONSE = {
    "unix_seconds": [1700000000, 1700003600, 1700007200],
    "ren_share": [45.5, 52.3, 61.8],
    "substitute": False,
}

SAMPLE_FORECAST_SUBSTITUTE = {
    "unix_seconds": [1700000000, 1700003600],
    "ren_share": [40.0, 45.0],
    "substitute": True,
}

SAMPLE_SIGNAL_RESPONSE = {
    "unix_seconds": [1700000000, 1700003600, 1700007200],
    "signal": [0, 1, -1],
    "substitute": False,
}

SAMPLE_SIGNAL_SUBSTITUTE = {
    "unix_seconds": [1700000000, 1700003600],
    "signal": [0, 0],
    "substitute": True,
}

SAMPLE_POWER_RESPONSE = {
    "unix_seconds": [1700000000, 1700003600],
    "solar": [5000.0, 8000.0],
    "wind_onshore": [15000.0, 12000.0],
    "wind_offshore": [3000.0, 4000.0],
    "nuclear": [4000.0, 4000.0],
    "lignite": [8000.0, 7000.0],
    "coal": [5000.0, 4000.0],
    "gas": [6000.0, 5000.0],
}


class TestRenewableShareForecast:
    """Tests for RenewableShareForecast model."""

    def test_parse_response(self) -> None:
        """Test parsing API response."""
        forecast = RenewableShareForecast(**SAMPLE_FORECAST_RESPONSE)

        assert len(forecast.unix_seconds) == 3
        assert len(forecast.ren_share) == 3
        assert forecast.ren_share[0] == 45.5
        assert forecast.substitute is False
        assert forecast.quality == DataQuality.LIVE

    def test_substitute_quality(self) -> None:
        """Test substitute flag affects quality."""
        forecast = RenewableShareForecast(**SAMPLE_FORECAST_SUBSTITUTE)

        assert forecast.substitute is True
        assert forecast.quality == DataQuality.SUBSTITUTE

    def test_get_share_at(self) -> None:
        """Test getting share at specific timestamp."""
        forecast = RenewableShareForecast(**SAMPLE_FORECAST_RESPONSE)

        # Exact match
        assert forecast.get_share_at(1700000000) == 45.5
        assert forecast.get_share_at(1700003600) == 52.3

        # After last timestamp
        assert forecast.get_share_at(1700010000) == 61.8

    def test_get_forecast_window(self) -> None:
        """Test getting forecast within a window."""
        forecast = RenewableShareForecast(**SAMPLE_FORECAST_RESPONSE)

        window = forecast.get_forecast_window(1700000000, 1700007200)
        assert len(window) == 2
        assert window[0] == (1700000000, 45.5)
        assert window[1] == (1700003600, 52.3)

    def test_invalid_share_values(self) -> None:
        """Test validation rejects invalid share values."""
        with pytest.raises(ValueError, match="must be 0-100"):
            RenewableShareForecast(
                unix_seconds=[1700000000],
                ren_share=[150.0],  # Invalid: > 100
            )

    def test_empty_forecast(self) -> None:
        """Test empty forecast returns None for lookups."""
        forecast = RenewableShareForecast(unix_seconds=[], ren_share=[])

        assert forecast.get_share_at(1700000000) is None


class TestGridSignal:
    """Tests for GridSignal model."""

    def test_parse_response(self) -> None:
        """Test parsing API response."""
        signal = GridSignal(**SAMPLE_SIGNAL_RESPONSE)

        assert len(signal.unix_seconds) == 3
        assert signal.signal == [0, 1, -1]
        assert signal.substitute is False
        assert signal.quality == DataQuality.LIVE

    def test_substitute_quality(self) -> None:
        """Test substitute flag affects quality."""
        signal = GridSignal(**SAMPLE_SIGNAL_SUBSTITUTE)

        assert signal.substitute is True
        assert signal.quality == DataQuality.SUBSTITUTE

    def test_get_signal_at(self) -> None:
        """Test getting signal at specific timestamp."""
        signal = GridSignal(**SAMPLE_SIGNAL_RESPONSE)

        assert signal.get_signal_at(1700000000) == 0
        assert signal.get_signal_at(1700003600) == 1
        assert signal.get_signal_at(1700007200) == -1


class TestPublicPower:
    """Tests for PublicPower model."""

    def test_from_energy_charts_response(self) -> None:
        """Test parsing Energy-Charts response format."""
        power = PublicPower.from_energy_charts_response(SAMPLE_POWER_RESPONSE)

        assert len(power.unix_seconds) == 2
        assert "solar" in power.production_types
        assert "wind_onshore" in power.production_types
        assert power.data["solar"] == [5000.0, 8000.0]

    def test_get_total_production(self) -> None:
        """Test total production calculation."""
        power = PublicPower.from_energy_charts_response(SAMPLE_POWER_RESPONSE)

        total = power.get_total_production(1700000000)
        expected = 5000 + 15000 + 3000 + 4000 + 8000 + 5000 + 6000  # Excludes import/export
        assert total == expected

    def test_get_renewable_production(self) -> None:
        """Test renewable production calculation."""
        power = PublicPower.from_energy_charts_response(SAMPLE_POWER_RESPONSE)

        renewable = power.get_renewable_production(1700000000)
        expected = 5000 + 15000 + 3000  # solar + wind_onshore + wind_offshore
        assert renewable == expected


class TestGridData:
    """Tests for GridData model."""

    def test_quality_live(self) -> None:
        """Test quality when all data is live."""
        data = GridData(
            forecast=RenewableShareForecast(**SAMPLE_FORECAST_RESPONSE),
            signal=GridSignal(**SAMPLE_SIGNAL_RESPONSE),
            fetched_at=datetime.now(),
        )

        assert data.quality == DataQuality.LIVE
        assert not data.is_degraded

    def test_quality_degraded(self) -> None:
        """Test quality when some data is substitute."""
        data = GridData(
            forecast=RenewableShareForecast(**SAMPLE_FORECAST_SUBSTITUTE),
            signal=GridSignal(**SAMPLE_SIGNAL_RESPONSE),
            fetched_at=datetime.now(),
        )

        assert data.quality == DataQuality.DEGRADED
        assert data.is_degraded

    def test_quality_unavailable(self) -> None:
        """Test quality when no data is available."""
        data = GridData(fetched_at=datetime.now())

        assert data.quality == DataQuality.UNAVAILABLE


class TestRateLimitState:
    """Tests for RateLimitState."""

    def test_initial_state(self) -> None:
        """Test initial state is not blocked."""
        state = RateLimitState()

        assert not state.is_blocked()
        assert state.time_until_ready() == 0.0

    def test_record_429(self) -> None:
        """Test recording a 429 response."""
        state = RateLimitState()
        state.record_429(30.0)

        assert state.is_blocked()
        assert state.time_until_ready() > 0
        assert state.consecutive_429s == 1

    def test_record_success_resets(self) -> None:
        """Test success resets consecutive 429 count."""
        state = RateLimitState()
        state.record_429(1.0)
        state.consecutive_429s = 3
        state.record_success()

        assert state.consecutive_429s == 0


class TestGridCache:
    """Tests for GridCache."""

    def test_cache_miss(self) -> None:
        """Test cache miss returns None."""
        cache = GridCache(ttl_seconds=60)

        assert cache.get_forecast("de") is None
        assert cache.stats.misses == 1

    def test_cache_hit(self) -> None:
        """Test cache hit returns data."""
        cache = GridCache(ttl_seconds=60)
        forecast = RenewableShareForecast(**SAMPLE_FORECAST_RESPONSE)

        cache.set_forecast("de", forecast)
        entry = cache.get_forecast("de")

        assert entry is not None
        assert entry.data == forecast
        assert cache.stats.hits == 1

    def test_cache_ttl(self) -> None:
        """Test cache respects TTL."""
        cache = GridCache(ttl_seconds=1)
        forecast = RenewableShareForecast(**SAMPLE_FORECAST_RESPONSE)

        cache.set_forecast("de", forecast)

        # Should hit
        assert cache.get_forecast("de") is not None

        # Wait for TTL to expire
        time.sleep(1.1)

        # Should miss after TTL
        assert cache.get_forecast("de") is None

    def test_rate_limit_tracking(self) -> None:
        """Test rate limit tracking."""
        cache = GridCache()

        assert not cache.is_rate_limited("/forecast")

        cache.record_rate_limit("/forecast", 5.0)

        assert cache.is_rate_limited("/forecast")
        assert cache.get_retry_after("/forecast") > 0

    def test_invalidate_country(self) -> None:
        """Test invalidating cache for a specific country."""
        cache = GridCache()
        forecast = RenewableShareForecast(**SAMPLE_FORECAST_RESPONSE)

        cache.set_forecast("de", forecast)
        cache.set_forecast("at", forecast)

        cache.invalidate("de")

        assert cache.get_forecast("de") is None
        assert cache.get_forecast("at") is not None

    def test_invalidate_all(self) -> None:
        """Test invalidating all cache entries."""
        cache = GridCache()
        forecast = RenewableShareForecast(**SAMPLE_FORECAST_RESPONSE)

        cache.set_forecast("de", forecast)
        cache.set_forecast("at", forecast)

        cache.invalidate()

        assert cache.get_forecast("de") is None
        assert cache.get_forecast("at") is None

    def test_get_combined(self) -> None:
        """Test getting combined grid data from cache."""
        cache = GridCache()
        forecast = RenewableShareForecast(**SAMPLE_FORECAST_RESPONSE)
        signal = GridSignal(**SAMPLE_SIGNAL_RESPONSE)

        cache.set_forecast("de", forecast)
        cache.set_signal("de", signal)

        combined = cache.get_combined("de")

        assert combined is not None
        assert combined.forecast == forecast
        assert combined.signal == signal

    def test_cache_stats(self) -> None:
        """Test cache statistics."""
        cache = GridCache()
        forecast = RenewableShareForecast(**SAMPLE_FORECAST_RESPONSE)

        cache.get_forecast("de")  # Miss
        cache.set_forecast("de", forecast)
        cache.get_forecast("de")  # Hit
        cache.get_forecast("de")  # Hit

        assert cache.stats.hits == 2
        assert cache.stats.misses == 1
        assert cache.stats.hit_rate == pytest.approx(66.67, rel=0.1)


class TestEnergyChartsClient:
    """Tests for EnergyChartsClient."""

    @pytest.mark.asyncio
    async def test_get_renewable_forecast(self, httpx_mock: HTTPXMock) -> None:
        """Test fetching renewable forecast."""
        httpx_mock.add_response(
            url="https://api.energy-charts.info/ren_share_forecast?country=de",
            json=SAMPLE_FORECAST_RESPONSE,
        )

        async with EnergyChartsClient() as client:
            forecast = await client.get_renewable_forecast()

        assert len(forecast.unix_seconds) == 3
        assert forecast.ren_share[0] == 45.5
        assert not forecast.substitute

    @pytest.mark.asyncio
    async def test_get_signal(self, httpx_mock: HTTPXMock) -> None:
        """Test fetching grid signal."""
        httpx_mock.add_response(
            url="https://api.energy-charts.info/signal?country=de",
            json=SAMPLE_SIGNAL_RESPONSE,
        )

        async with EnergyChartsClient() as client:
            signal = await client.get_signal()

        assert signal.signal == [0, 1, -1]
        assert not signal.substitute

    @pytest.mark.asyncio
    async def test_get_public_power(self, httpx_mock: HTTPXMock) -> None:
        """Test fetching public power data."""
        httpx_mock.add_response(
            url="https://api.energy-charts.info/public_power?country=de",
            json=SAMPLE_POWER_RESPONSE,
        )

        async with EnergyChartsClient() as client:
            power = await client.get_public_power()

        assert "solar" in power.production_types
        assert power.data["solar"] == [5000.0, 8000.0]

    @pytest.mark.asyncio
    async def test_get_grid_data_combined(self, httpx_mock: HTTPXMock) -> None:
        """Test fetching combined grid data."""
        httpx_mock.add_response(
            url="https://api.energy-charts.info/ren_share_forecast?country=de",
            json=SAMPLE_FORECAST_RESPONSE,
        )
        httpx_mock.add_response(
            url="https://api.energy-charts.info/signal?country=de",
            json=SAMPLE_SIGNAL_RESPONSE,
        )
        httpx_mock.add_response(
            url="https://api.energy-charts.info/public_power?country=de",
            json=SAMPLE_POWER_RESPONSE,
        )

        async with EnergyChartsClient() as client:
            data = await client.get_grid_data()

        assert data.forecast is not None
        assert data.signal is not None
        assert data.power is not None
        assert data.quality == DataQuality.LIVE

    @pytest.mark.asyncio
    async def test_caching(self, httpx_mock: HTTPXMock) -> None:
        """Test response caching."""
        httpx_mock.add_response(
            url="https://api.energy-charts.info/ren_share_forecast?country=de",
            json=SAMPLE_FORECAST_RESPONSE,
        )

        async with EnergyChartsClient() as client:
            # First call - should make request
            forecast1 = await client.get_renewable_forecast()

            # Second call - should use cache
            forecast2 = await client.get_renewable_forecast()

            assert forecast1 == forecast2
            # Only one request should have been made
            assert len(httpx_mock.get_requests()) == 1

    @pytest.mark.asyncio
    async def test_cache_bypass(self, httpx_mock: HTTPXMock) -> None:
        """Test bypassing cache."""
        httpx_mock.add_response(
            url="https://api.energy-charts.info/ren_share_forecast?country=de",
            json=SAMPLE_FORECAST_RESPONSE,
        )
        httpx_mock.add_response(
            url="https://api.energy-charts.info/ren_share_forecast?country=de",
            json=SAMPLE_FORECAST_RESPONSE,
        )

        async with EnergyChartsClient() as client:
            await client.get_renewable_forecast()
            await client.get_renewable_forecast(use_cache=False)

            # Two requests should have been made
            assert len(httpx_mock.get_requests()) == 2

    @pytest.mark.asyncio
    async def test_rate_limit_429(self, httpx_mock: HTTPXMock) -> None:
        """Test handling HTTP 429 rate limit."""
        httpx_mock.add_response(
            url="https://api.energy-charts.info/ren_share_forecast?country=de",
            status_code=429,
            headers={"Retry-After": "30"},
        )

        async with EnergyChartsClient() as client:
            with pytest.raises(RateLimitError) as exc_info:
                await client.get_renewable_forecast()

            assert exc_info.value.retry_after == 30.0

    @pytest.mark.asyncio
    async def test_rate_limit_blocks_subsequent(self, httpx_mock: HTTPXMock) -> None:
        """Test that rate limit blocks subsequent requests."""
        httpx_mock.add_response(
            url="https://api.energy-charts.info/ren_share_forecast?country=de",
            status_code=429,
            headers={"Retry-After": "60"},
        )

        async with EnergyChartsClient() as client:
            # First request gets rate limited
            with pytest.raises(RateLimitError):
                await client.get_renewable_forecast()

            # Second request should be blocked immediately
            with pytest.raises(RateLimitError) as exc_info:
                await client.get_renewable_forecast(use_cache=False)

            # Should not have made a second request
            assert len(httpx_mock.get_requests()) == 1
            assert exc_info.value.retry_after > 0

    @pytest.mark.asyncio
    async def test_api_error(self, httpx_mock: HTTPXMock) -> None:
        """Test handling API errors."""
        httpx_mock.add_response(
            url="https://api.energy-charts.info/ren_share_forecast?country=de",
            status_code=500,
            json={"error": "Internal server error"},
        )

        async with EnergyChartsClient() as client:
            with pytest.raises(APIError) as exc_info:
                await client.get_renewable_forecast()

            assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_substitute_flag_handling(self, httpx_mock: HTTPXMock) -> None:
        """Test substitute flag is properly propagated."""
        httpx_mock.add_response(
            url="https://api.energy-charts.info/ren_share_forecast?country=de",
            json=SAMPLE_FORECAST_SUBSTITUTE,
        )
        httpx_mock.add_response(
            url="https://api.energy-charts.info/signal?country=de",
            json=SAMPLE_SIGNAL_RESPONSE,
        )
        httpx_mock.add_response(
            url="https://api.energy-charts.info/public_power?country=de",
            json=SAMPLE_POWER_RESPONSE,
        )

        async with EnergyChartsClient() as client:
            data = await client.get_grid_data()

        assert data.forecast is not None
        assert data.forecast.substitute is True
        assert data.is_degraded is True
        assert data.quality == DataQuality.DEGRADED

    @pytest.mark.asyncio
    async def test_is_data_degraded(self, httpx_mock: HTTPXMock) -> None:
        """Test is_data_degraded method."""
        httpx_mock.add_response(
            url="https://api.energy-charts.info/ren_share_forecast?country=de",
            json=SAMPLE_FORECAST_SUBSTITUTE,
        )

        async with EnergyChartsClient() as client:
            await client.get_renewable_forecast()

            assert client.is_data_degraded() is True

    @pytest.mark.asyncio
    async def test_refresh_cache(self, httpx_mock: HTTPXMock) -> None:
        """Test cache refresh."""
        httpx_mock.add_response(
            url="https://api.energy-charts.info/ren_share_forecast?country=de",
            json=SAMPLE_FORECAST_RESPONSE,
        )
        httpx_mock.add_response(
            url="https://api.energy-charts.info/signal?country=de",
            json=SAMPLE_SIGNAL_RESPONSE,
        )
        httpx_mock.add_response(
            url="https://api.energy-charts.info/public_power?country=de",
            json=SAMPLE_POWER_RESPONSE,
        )
        # Duplicate responses for refresh
        httpx_mock.add_response(
            url="https://api.energy-charts.info/ren_share_forecast?country=de",
            json=SAMPLE_FORECAST_RESPONSE,
        )
        httpx_mock.add_response(
            url="https://api.energy-charts.info/signal?country=de",
            json=SAMPLE_SIGNAL_RESPONSE,
        )
        httpx_mock.add_response(
            url="https://api.energy-charts.info/public_power?country=de",
            json=SAMPLE_POWER_RESPONSE,
        )

        async with EnergyChartsClient() as client:
            await client.get_grid_data()
            await client.refresh_cache()

            # Should have made 6 requests (3 initial + 3 refresh)
            assert len(httpx_mock.get_requests()) == 6

    @pytest.mark.asyncio
    async def test_custom_country(self, httpx_mock: HTTPXMock) -> None:
        """Test fetching data for different country."""
        httpx_mock.add_response(
            url="https://api.energy-charts.info/ren_share_forecast?country=at",
            json=SAMPLE_FORECAST_RESPONSE,
        )

        async with EnergyChartsClient(country="de") as client:
            forecast = await client.get_renewable_forecast(country="at")

        assert forecast is not None
        request = httpx_mock.get_requests()[0]
        assert "country=at" in str(request.url)

    @pytest.mark.asyncio
    async def test_get_cache_entry(self, httpx_mock: HTTPXMock) -> None:
        """Test getting cache entry for debugging."""
        httpx_mock.add_response(
            url="https://api.energy-charts.info/ren_share_forecast?country=de",
            json=SAMPLE_FORECAST_RESPONSE,
        )

        async with EnergyChartsClient() as client:
            await client.get_renewable_forecast()

            entry = client.get_cache_entry("forecast")

            assert entry is not None
            assert isinstance(entry, CacheEntry)
            assert entry.data.ren_share == SAMPLE_FORECAST_RESPONSE["ren_share"]

    @pytest.mark.asyncio
    async def test_context_manager_required(self) -> None:
        """Test that client must be used as context manager."""
        client = EnergyChartsClient()

        with pytest.raises(RuntimeError, match="Client not initialized"):
            await client.get_renewable_forecast()

    @pytest.mark.asyncio
    async def test_partial_grid_data_on_error(self, httpx_mock: HTTPXMock) -> None:
        """Test that partial data is returned when some endpoints fail."""
        httpx_mock.add_response(
            url="https://api.energy-charts.info/ren_share_forecast?country=de",
            json=SAMPLE_FORECAST_RESPONSE,
        )
        httpx_mock.add_response(
            url="https://api.energy-charts.info/signal?country=de",
            status_code=500,
        )
        httpx_mock.add_response(
            url="https://api.energy-charts.info/public_power?country=de",
            json=SAMPLE_POWER_RESPONSE,
        )

        async with EnergyChartsClient() as client:
            data = await client.get_grid_data()

        assert data.forecast is not None
        assert data.signal is None  # Failed
        assert data.power is not None


class TestCacheTTLBehavior:
    """Tests specifically for cache TTL behavior."""

    @pytest.mark.asyncio
    async def test_cache_ttl_custom(self, httpx_mock: HTTPXMock) -> None:
        """Test custom cache TTL."""
        httpx_mock.add_response(
            url="https://api.energy-charts.info/ren_share_forecast?country=de",
            json=SAMPLE_FORECAST_RESPONSE,
        )
        httpx_mock.add_response(
            url="https://api.energy-charts.info/ren_share_forecast?country=de",
            json=SAMPLE_FORECAST_RESPONSE,
        )

        # Very short TTL for testing
        async with EnergyChartsClient(cache_ttl=1) as client:
            await client.get_renewable_forecast()

            # Wait for TTL to expire
            await asyncio.sleep(1.1)

            # Should make new request
            await client.get_renewable_forecast()

            assert len(httpx_mock.get_requests()) == 2

    def test_cache_entry_expiry(self) -> None:
        """Test cache entry expiry detection."""
        cache = GridCache(ttl_seconds=1)
        forecast = RenewableShareForecast(**SAMPLE_FORECAST_RESPONSE)

        entry = cache.set_forecast("de", forecast)

        assert not entry.is_expired

        time.sleep(1.1)

        assert entry.is_expired

    def test_cache_entry_age(self) -> None:
        """Test cache entry age calculation."""
        cache = GridCache()
        forecast = RenewableShareForecast(**SAMPLE_FORECAST_RESPONSE)

        entry = cache.set_forecast("de", forecast)

        # Age should be very small initially
        assert entry.age_seconds < 1.0

        time.sleep(0.5)

        # Age should have increased
        assert entry.age_seconds >= 0.5


class TestRetryAfterBehavior:
    """Tests for Retry-After header handling."""

    @pytest.mark.asyncio
    async def test_retry_after_header_parsed(self, httpx_mock: HTTPXMock) -> None:
        """Test Retry-After header is parsed correctly."""
        httpx_mock.add_response(
            url="https://api.energy-charts.info/ren_share_forecast?country=de",
            status_code=429,
            headers={"Retry-After": "45"},
        )

        async with EnergyChartsClient() as client:
            with pytest.raises(RateLimitError) as exc_info:
                await client.get_renewable_forecast()

            assert exc_info.value.retry_after == 45.0

    @pytest.mark.asyncio
    async def test_retry_after_default(self, httpx_mock: HTTPXMock) -> None:
        """Test default Retry-After when header is missing."""
        httpx_mock.add_response(
            url="https://api.energy-charts.info/ren_share_forecast?country=de",
            status_code=429,
        )

        async with EnergyChartsClient() as client:
            with pytest.raises(RateLimitError) as exc_info:
                await client.get_renewable_forecast()

            # Default is 60 seconds
            assert exc_info.value.retry_after == 60.0

    @pytest.mark.asyncio
    async def test_wait_for_rate_limit(self, httpx_mock: HTTPXMock) -> None:
        """Test waiting for rate limit to clear."""
        httpx_mock.add_response(
            url="https://api.energy-charts.info/ren_share_forecast?country=de",
            status_code=429,
            headers={"Retry-After": "1"},
        )
        httpx_mock.add_response(
            url="https://api.energy-charts.info/ren_share_forecast?country=de",
            json=SAMPLE_FORECAST_RESPONSE,
        )

        async with EnergyChartsClient() as client:
            with pytest.raises(RateLimitError):
                await client.get_renewable_forecast()

            # Wait for rate limit
            await client.wait_for_rate_limit("/ren_share_forecast")

            # Should now succeed
            forecast = await client.get_renewable_forecast(use_cache=False)
            assert forecast is not None
