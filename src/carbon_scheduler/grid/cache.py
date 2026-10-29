"""Response caching for Energy-Charts API with TTL support.

Provides caching with configurable TTL that respects API rate limits.
Default TTL is 30 minutes as per F-03 requirements.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime

import structlog
from cachetools import TTLCache

from carbon_scheduler.grid.models import (
    DataQuality,
    GridData,
    GridSignal,
    PublicPower,
    RenewableShareForecast,
)

logger = structlog.get_logger(__name__)

DEFAULT_TTL_SECONDS = 30 * 60  # 30 minutes
DEFAULT_MAX_SIZE = 100


@dataclass
class CacheEntry[T]:
    """A cached response with metadata."""

    data: T
    fetched_at: datetime
    expires_at: datetime
    quality: DataQuality = DataQuality.LIVE

    @property
    def is_expired(self) -> bool:
        """Check if this entry has expired."""
        return datetime.now() >= self.expires_at

    @property
    def age_seconds(self) -> float:
        """Get age of this entry in seconds."""
        return (datetime.now() - self.fetched_at).total_seconds()


@dataclass
class RateLimitState:
    """Track rate limiting state for an endpoint."""

    retry_after: float = 0.0
    retry_until: float = 0.0
    consecutive_429s: int = 0

    def is_blocked(self) -> bool:
        """Check if we should block requests due to rate limiting."""
        return time.monotonic() < self.retry_until

    def time_until_ready(self) -> float:
        """Get seconds until we can make another request."""
        remaining = self.retry_until - time.monotonic()
        return max(0.0, remaining)

    def record_429(self, retry_after: float) -> None:
        """Record a 429 response."""
        self.consecutive_429s += 1
        self.retry_after = retry_after
        self.retry_until = time.monotonic() + retry_after
        logger.warning(
            "rate_limited",
            retry_after=retry_after,
            consecutive_429s=self.consecutive_429s,
        )

    def record_success(self) -> None:
        """Record a successful response."""
        self.consecutive_429s = 0


@dataclass
class CacheStats:
    """Statistics for cache operations."""

    hits: int = 0
    misses: int = 0
    rate_limit_blocks: int = 0
    errors: int = 0

    @property
    def hit_rate(self) -> float:
        """Get cache hit rate as a percentage."""
        total = self.hits + self.misses
        return (self.hits / total * 100) if total > 0 else 0.0


@dataclass
class GridCache:
    """Cache for grid data with TTL and rate limit awareness.

    Caches responses from Energy-Charts API endpoints with configurable TTL.
    Tracks rate limiting state per endpoint to avoid hammering the API.
    """

    ttl_seconds: int = DEFAULT_TTL_SECONDS
    max_size: int = DEFAULT_MAX_SIZE
    _forecast_cache: TTLCache[str, CacheEntry[RenewableShareForecast]] = field(
        default_factory=lambda: TTLCache(maxsize=DEFAULT_MAX_SIZE, ttl=DEFAULT_TTL_SECONDS)
    )
    _signal_cache: TTLCache[str, CacheEntry[GridSignal]] = field(
        default_factory=lambda: TTLCache(maxsize=DEFAULT_MAX_SIZE, ttl=DEFAULT_TTL_SECONDS)
    )
    _power_cache: TTLCache[str, CacheEntry[PublicPower]] = field(
        default_factory=lambda: TTLCache(maxsize=DEFAULT_MAX_SIZE, ttl=DEFAULT_TTL_SECONDS)
    )
    _rate_limits: dict[str, RateLimitState] = field(default_factory=dict)
    _stats: CacheStats = field(default_factory=CacheStats)

    def __post_init__(self) -> None:
        """Initialize caches with configured TTL."""
        self._forecast_cache = TTLCache(maxsize=self.max_size, ttl=self.ttl_seconds)
        self._signal_cache = TTLCache(maxsize=self.max_size, ttl=self.ttl_seconds)
        self._power_cache = TTLCache(maxsize=self.max_size, ttl=self.ttl_seconds)

    @property
    def stats(self) -> CacheStats:
        """Get cache statistics."""
        return self._stats

    def _get_rate_limit(self, endpoint: str) -> RateLimitState:
        """Get rate limit state for an endpoint."""
        if endpoint not in self._rate_limits:
            self._rate_limits[endpoint] = RateLimitState()
        return self._rate_limits[endpoint]

    def is_rate_limited(self, endpoint: str) -> bool:
        """Check if an endpoint is currently rate limited."""
        return self._get_rate_limit(endpoint).is_blocked()

    def get_retry_after(self, endpoint: str) -> float:
        """Get seconds until endpoint can be called again."""
        return self._get_rate_limit(endpoint).time_until_ready()

    def record_rate_limit(self, endpoint: str, retry_after: float) -> None:
        """Record a rate limit (429) response for an endpoint."""
        self._get_rate_limit(endpoint).record_429(retry_after)
        self._stats.rate_limit_blocks += 1

    def record_success(self, endpoint: str) -> None:
        """Record a successful response for an endpoint."""
        self._get_rate_limit(endpoint).record_success()

    def _make_key(self, country: str) -> str:
        """Create cache key from country code."""
        return country.lower()

    def get_forecast(self, country: str) -> CacheEntry[RenewableShareForecast] | None:
        """Get cached forecast data."""
        key = self._make_key(country)
        entry = self._forecast_cache.get(key)
        if entry is not None:
            self._stats.hits += 1
            logger.debug("cache_hit", endpoint="forecast", country=country)
        else:
            self._stats.misses += 1
            logger.debug("cache_miss", endpoint="forecast", country=country)
        return entry

    def set_forecast(
        self,
        country: str,
        data: RenewableShareForecast,
        ttl_override: int | None = None,
    ) -> CacheEntry[RenewableShareForecast]:
        """Cache forecast data."""
        key = self._make_key(country)
        ttl = ttl_override if ttl_override is not None else self.ttl_seconds
        now = datetime.now()
        entry = CacheEntry(
            data=data,
            fetched_at=now,
            expires_at=datetime.fromtimestamp(now.timestamp() + ttl),
            quality=data.quality,
        )
        self._forecast_cache[key] = entry
        logger.debug(
            "cache_set",
            endpoint="forecast",
            country=country,
            ttl=ttl,
            quality=entry.quality.value,
        )
        return entry

    def get_signal(self, country: str) -> CacheEntry[GridSignal] | None:
        """Get cached signal data."""
        key = self._make_key(country)
        entry = self._signal_cache.get(key)
        if entry is not None:
            self._stats.hits += 1
            logger.debug("cache_hit", endpoint="signal", country=country)
        else:
            self._stats.misses += 1
            logger.debug("cache_miss", endpoint="signal", country=country)
        return entry

    def set_signal(
        self,
        country: str,
        data: GridSignal,
        ttl_override: int | None = None,
    ) -> CacheEntry[GridSignal]:
        """Cache signal data."""
        key = self._make_key(country)
        ttl = ttl_override if ttl_override is not None else self.ttl_seconds
        now = datetime.now()
        entry = CacheEntry(
            data=data,
            fetched_at=now,
            expires_at=datetime.fromtimestamp(now.timestamp() + ttl),
            quality=data.quality,
        )
        self._signal_cache[key] = entry
        logger.debug(
            "cache_set",
            endpoint="signal",
            country=country,
            ttl=ttl,
            quality=entry.quality.value,
        )
        return entry

    def get_power(self, country: str) -> CacheEntry[PublicPower] | None:
        """Get cached power data."""
        key = self._make_key(country)
        entry = self._power_cache.get(key)
        if entry is not None:
            self._stats.hits += 1
            logger.debug("cache_hit", endpoint="power", country=country)
        else:
            self._stats.misses += 1
            logger.debug("cache_miss", endpoint="power", country=country)
        return entry

    def set_power(
        self,
        country: str,
        data: PublicPower,
        ttl_override: int | None = None,
    ) -> CacheEntry[PublicPower]:
        """Cache power data."""
        key = self._make_key(country)
        ttl = ttl_override if ttl_override is not None else self.ttl_seconds
        now = datetime.now()
        entry = CacheEntry(
            data=data,
            fetched_at=now,
            expires_at=datetime.fromtimestamp(now.timestamp() + ttl),
            quality=DataQuality.LIVE,
        )
        self._power_cache[key] = entry
        logger.debug("cache_set", endpoint="power", country=country, ttl=ttl)
        return entry

    def get_combined(self, country: str) -> GridData | None:
        """Get combined grid data from cache if all required data is available."""
        forecast_entry = self.get_forecast(country)
        signal_entry = self.get_signal(country)
        power_entry = self.get_power(country)

        if forecast_entry is None:
            return None

        oldest_fetch = forecast_entry.fetched_at
        if signal_entry is not None and signal_entry.fetched_at < oldest_fetch:
            oldest_fetch = signal_entry.fetched_at
        if power_entry is not None and power_entry.fetched_at < oldest_fetch:
            oldest_fetch = power_entry.fetched_at

        return GridData(
            forecast=forecast_entry.data if forecast_entry else None,
            signal=signal_entry.data if signal_entry else None,
            power=power_entry.data if power_entry else None,
            fetched_at=oldest_fetch,
            country=country,
        )

    def invalidate(self, country: str | None = None) -> None:
        """Invalidate cached data.

        Args:
            country: If provided, invalidate only data for this country.
                    If None, invalidate all cached data.
        """
        if country is not None:
            key = self._make_key(country)
            self._forecast_cache.pop(key, None)
            self._signal_cache.pop(key, None)
            self._power_cache.pop(key, None)
            logger.info("cache_invalidated", country=country)
        else:
            self._forecast_cache.clear()
            self._signal_cache.clear()
            self._power_cache.clear()
            logger.info("cache_invalidated_all")

    def clear_rate_limits(self) -> None:
        """Clear all rate limit state."""
        self._rate_limits.clear()
        logger.info("rate_limits_cleared")
