"""Energy-Charts grid client module.

Provides async access to Energy-Charts API for:
- Renewable share forecasts
- Grid signal data
- Public power production data

All with caching, rate limit handling, and degraded data flagging.
"""

from carbon_scheduler.grid.cache import (
    CacheEntry,
    CacheStats,
    GridCache,
    RateLimitState,
)
from carbon_scheduler.grid.client import (
    APIError,
    EnergyChartsClient,
    EnergyChartsError,
    RateLimitError,
)
from carbon_scheduler.grid.models import (
    DataQuality,
    GridData,
    GridSignal,
    ProductionType,
    PublicPower,
    RenewableShareForecast,
)

__all__ = [
    # Client
    "EnergyChartsClient",
    "EnergyChartsError",
    "RateLimitError",
    "APIError",
    # Models
    "GridData",
    "GridSignal",
    "RenewableShareForecast",
    "PublicPower",
    "ProductionType",
    "DataQuality",
    # Cache
    "GridCache",
    "CacheEntry",
    "CacheStats",
    "RateLimitState",
]
