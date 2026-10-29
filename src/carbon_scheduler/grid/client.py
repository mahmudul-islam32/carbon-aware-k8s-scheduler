"""Energy-Charts API client with async support and caching.

Provides access to:
- /ren_share_forecast - Renewable share forecast
- /signal - Grid signal data
- /public_power - Production per type in MW

All endpoints support country=de (Germany) as the primary use case.
"""

import asyncio
from datetime import datetime
from typing import Any

import httpx
import structlog

from carbon_scheduler.grid.cache import DEFAULT_TTL_SECONDS, CacheEntry, GridCache
from carbon_scheduler.grid.models import (
    DataQuality,
    GridData,
    GridSignal,
    PublicPower,
    RenewableShareForecast,
)

logger = structlog.get_logger(__name__)

DEFAULT_BASE_URL = "https://api.energy-charts.info"
DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRY_AFTER = 60.0
MAX_RETRIES = 3


class EnergyChartsError(Exception):
    """Base exception for Energy-Charts client errors."""

    pass


class RateLimitError(EnergyChartsError):
    """Raised when API rate limit is exceeded."""

    def __init__(self, retry_after: float, message: str = "Rate limit exceeded"):
        self.retry_after = retry_after
        super().__init__(f"{message}. Retry after {retry_after}s")


class APIError(EnergyChartsError):
    """Raised for API errors other than rate limiting."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"API error {status_code}: {message}")


class EnergyChartsClient:
    """Async client for Energy-Charts API.

    Handles:
    - Response caching with configurable TTL (default 30 minutes)
    - Rate limit handling with Retry-After header support
    - Substitute/degraded data flagging
    - Both v1 (unix_seconds) and v2 (ISO 8601) formats

    Usage:
        async with EnergyChartsClient() as client:
            data = await client.get_grid_data("de")
            print(data.forecast.ren_share)
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        cache_ttl: int = DEFAULT_TTL_SECONDS,
        country: str = "de",
        client: httpx.AsyncClient | None = None,
    ):
        """Initialize the Energy-Charts client.

        Args:
            base_url: Base URL for the API
            timeout: Request timeout in seconds
            cache_ttl: Cache TTL in seconds (default 30 minutes)
            country: Default country code
            client: Optional pre-configured httpx client (for testing)
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.country = country
        self._cache = GridCache(ttl_seconds=cache_ttl)
        self._external_client = client is not None
        self._client = client
        self._owned_client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "EnergyChartsClient":
        """Enter async context."""
        if self._client is None:
            self._owned_client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={"Accept": "application/json"},
            )
            self._client = self._owned_client
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Exit async context."""
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None
            if not self._external_client:
                self._client = None

    @property
    def cache(self) -> GridCache:
        """Get the cache instance."""
        return self._cache

    def _get_client(self) -> httpx.AsyncClient:
        """Get the HTTP client, raising if not initialized."""
        if self._client is None:
            raise RuntimeError(
                "Client not initialized. Use 'async with EnergyChartsClient() as client:'"
            )
        return self._client

    async def _request(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an API request with rate limit handling.

        Args:
            endpoint: API endpoint path (e.g., "/ren_share_forecast")
            params: Query parameters

        Returns:
            Parsed JSON response

        Raises:
            RateLimitError: If rate limited and cannot retry
            APIError: For other API errors
        """
        client = self._get_client()

        if self._cache.is_rate_limited(endpoint):
            retry_after = self._cache.get_retry_after(endpoint)
            logger.warning(
                "request_blocked_rate_limit",
                endpoint=endpoint,
                retry_after=retry_after,
            )
            raise RateLimitError(retry_after, f"Endpoint {endpoint} is rate limited")

        retries = 0
        last_error: Exception | None = None

        while retries < MAX_RETRIES:
            try:
                logger.debug("api_request", endpoint=endpoint, params=params, attempt=retries + 1)

                response = await client.get(endpoint, params=params)

                if response.status_code == 429:
                    retry_after_header = response.headers.get("Retry-After")
                    retry_after = (
                        float(retry_after_header)
                        if retry_after_header
                        else DEFAULT_RETRY_AFTER
                    )
                    self._cache.record_rate_limit(endpoint, retry_after)
                    raise RateLimitError(retry_after)

                response.raise_for_status()

                self._cache.record_success(endpoint)
                data: dict[str, Any] = response.json()

                logger.debug(
                    "api_response",
                    endpoint=endpoint,
                    status=response.status_code,
                    substitute=data.get("substitute", False),
                )

                return data

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    raise
                logger.error(
                    "api_error",
                    endpoint=endpoint,
                    status=e.response.status_code,
                    error=str(e),
                )
                raise APIError(e.response.status_code, str(e)) from e

            except httpx.RequestError as e:
                retries += 1
                last_error = e
                logger.warning(
                    "api_request_error",
                    endpoint=endpoint,
                    error=str(e),
                    retry=retries,
                )
                if retries < MAX_RETRIES:
                    await asyncio.sleep(2**retries)

            except RateLimitError:
                raise

        raise EnergyChartsError(f"Max retries exceeded for {endpoint}") from last_error

    async def get_renewable_forecast(
        self,
        country: str | None = None,
        use_cache: bool = True,
    ) -> RenewableShareForecast:
        """Get renewable share forecast.

        Args:
            country: Country code (default: client's default country)
            use_cache: Whether to use cached data if available

        Returns:
            RenewableShareForecast with unix_seconds and ren_share arrays
        """
        country = country or self.country
        endpoint = "/ren_share_forecast"

        if use_cache:
            cached = self._cache.get_forecast(country)
            if cached is not None:
                logger.debug("using_cached_forecast", country=country, age=cached.age_seconds)
                return cached.data

        data = await self._request(endpoint, params={"country": country})

        forecast = RenewableShareForecast(
            unix_seconds=data.get("unix_seconds", []),
            ren_share=data.get("ren_share", []),
            substitute=data.get("substitute", False),
        )

        self._cache.set_forecast(country, forecast)

        if forecast.substitute:
            logger.warning("forecast_substitute_data", country=country)

        return forecast

    async def get_signal(
        self,
        country: str | None = None,
        use_cache: bool = True,
    ) -> GridSignal:
        """Get grid signal data.

        Args:
            country: Country code (default: client's default country)
            use_cache: Whether to use cached data if available

        Returns:
            GridSignal with unix_seconds and signal arrays
        """
        country = country or self.country
        endpoint = "/signal"

        if use_cache:
            cached = self._cache.get_signal(country)
            if cached is not None:
                logger.debug("using_cached_signal", country=country, age=cached.age_seconds)
                return cached.data

        data = await self._request(endpoint, params={"country": country})

        signal = GridSignal(
            unix_seconds=data.get("unix_seconds", []),
            signal=data.get("signal", []),
            substitute=data.get("substitute", False),
        )

        self._cache.set_signal(country, signal)

        if signal.substitute:
            logger.warning("signal_substitute_data", country=country)

        return signal

    async def get_public_power(
        self,
        country: str | None = None,
        use_cache: bool = True,
    ) -> PublicPower:
        """Get public power production data.

        Args:
            country: Country code (default: client's default country)
            use_cache: Whether to use cached data if available

        Returns:
            PublicPower with production data per type in MW
        """
        country = country or self.country
        endpoint = "/public_power"

        if use_cache:
            cached = self._cache.get_power(country)
            if cached is not None:
                logger.debug("using_cached_power", country=country, age=cached.age_seconds)
                return cached.data

        data = await self._request(endpoint, params={"country": country})

        power = PublicPower.from_energy_charts_response(data)

        self._cache.set_power(country, power)

        return power

    async def get_grid_data(
        self,
        country: str | None = None,
        use_cache: bool = True,
        include_signal: bool = True,
        include_power: bool = True,
    ) -> GridData:
        """Get combined grid data from all endpoints.

        Fetches forecast, signal, and power data concurrently.

        Args:
            country: Country code (default: client's default country)
            use_cache: Whether to use cached data if available
            include_signal: Whether to include grid signal data
            include_power: Whether to include power production data

        Returns:
            GridData combining all available data sources
        """
        country = country or self.country

        if use_cache:
            cached = self._cache.get_combined(country)
            if cached is not None and cached.forecast is not None:
                logger.debug("using_cached_grid_data", country=country)
                return cached

        forecast_task = self.get_renewable_forecast(country, use_cache)
        signal_task = self.get_signal(country, use_cache) if include_signal else None
        power_task = self.get_public_power(country, use_cache) if include_power else None

        forecast: RenewableShareForecast | None = None
        signal: GridSignal | None = None
        power: PublicPower | None = None

        try:
            forecast = await forecast_task
        except Exception as e:
            logger.error("grid_data_fetch_error", error=str(e), task="forecast")

        if signal_task is not None:
            try:
                signal = await signal_task
            except Exception as e:
                logger.error("grid_data_fetch_error", error=str(e), task="signal")

        if power_task is not None:
            try:
                power = await power_task
            except Exception as e:
                logger.error("grid_data_fetch_error", error=str(e), task="power")

        return GridData(
            forecast=forecast,
            signal=signal,
            power=power,
            fetched_at=datetime.now(),
            country=country,
        )

    async def refresh_cache(self, country: str | None = None) -> GridData:
        """Force refresh all cached data.

        Args:
            country: Country code (default: client's default country)

        Returns:
            Fresh GridData
        """
        country = country or self.country
        self._cache.invalidate(country)
        return await self.get_grid_data(country, use_cache=False)

    def get_cache_entry(
        self, endpoint: str, country: str | None = None
    ) -> CacheEntry[RenewableShareForecast] | CacheEntry[GridSignal] | CacheEntry[PublicPower] | None:
        """Get cache entry for debugging/monitoring.

        Args:
            endpoint: One of "forecast", "signal", "power"
            country: Country code

        Returns:
            CacheEntry if cached, None otherwise
        """
        country = country or self.country

        match endpoint:
            case "forecast":
                return self._cache.get_forecast(country)
            case "signal":
                return self._cache.get_signal(country)
            case "power":
                return self._cache.get_power(country)
            case _:
                return None

    def is_data_degraded(self, country: str | None = None) -> bool:
        """Check if cached data is degraded (has substitute flag).

        Args:
            country: Country code

        Returns:
            True if any cached data has substitute=True
        """
        country = country or self.country
        cached = self._cache.get_combined(country)
        return cached.is_degraded if cached else True

    async def wait_for_rate_limit(self, endpoint: str) -> None:
        """Wait until an endpoint is no longer rate limited.

        Args:
            endpoint: The rate-limited endpoint
        """
        wait_time = self._cache.get_retry_after(endpoint)
        if wait_time > 0:
            logger.info("waiting_for_rate_limit", endpoint=endpoint, seconds=wait_time)
            await asyncio.sleep(wait_time)


__all__ = [
    "EnergyChartsClient",
    "EnergyChartsError",
    "RateLimitError",
    "APIError",
    "GridData",
    "GridSignal",
    "RenewableShareForecast",
    "PublicPower",
    "DataQuality",
]
