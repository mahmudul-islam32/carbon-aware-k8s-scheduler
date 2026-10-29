"""Pydantic models for Energy-Charts API responses.

Supports both v1 (unix_seconds arrays) and v2 (ISO 8601) formats.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field, field_validator


class DataQuality(StrEnum):
    """Quality level of grid data."""

    LIVE = "live"
    SUBSTITUTE = "substitute"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class RenewableShareForecast(BaseModel):
    """Response model for /ren_share_forecast endpoint.

    Contains arrays of timestamps and renewable share percentages.
    When substitute=true, the forecast is degraded (fallback data).
    """

    unix_seconds: list[int] = Field(
        description="Unix timestamps for each forecast point",
    )
    ren_share: list[float] = Field(
        description="Renewable share percentage (0-100) for each timestamp",
    )
    substitute: bool = Field(
        default=False,
        description="True if this is fallback/substitute data",
    )

    @field_validator("ren_share")
    @classmethod
    def validate_ren_share(cls, v: list[float]) -> list[float]:
        """Validate renewable share values are in valid range."""
        for share in v:
            if not 0 <= share <= 100:
                raise ValueError(f"Renewable share must be 0-100, got {share}")
        return v

    @property
    def quality(self) -> DataQuality:
        """Get data quality based on substitute flag."""
        return DataQuality.SUBSTITUTE if self.substitute else DataQuality.LIVE

    def get_share_at(self, timestamp: int) -> float | None:
        """Get renewable share at a specific unix timestamp.

        Returns the share for the interval containing the timestamp,
        or None if timestamp is outside the forecast range.
        """
        if not self.unix_seconds:
            return None

        for i, ts in enumerate(self.unix_seconds):
            if ts >= timestamp:
                return self.ren_share[i] if i < len(self.ren_share) else None
            if i > 0 and self.unix_seconds[i - 1] <= timestamp < ts:
                return self.ren_share[i - 1]

        if timestamp >= self.unix_seconds[-1]:
            return self.ren_share[-1] if self.ren_share else None

        return None

    def get_forecast_window(
        self, start: int, end: int
    ) -> list[tuple[int, float]]:
        """Get forecast data points within a time window.

        Args:
            start: Start timestamp (inclusive)
            end: End timestamp (exclusive)

        Returns:
            List of (timestamp, share) tuples
        """
        return [
            (ts, share)
            for ts, share in zip(self.unix_seconds, self.ren_share, strict=False)
            if start <= ts < end
        ]


class GridSignal(BaseModel):
    """Response model for /signal endpoint.

    Contains grid signal data indicating optimal times for consumption.
    """

    unix_seconds: list[int] = Field(
        description="Unix timestamps for each signal point",
    )
    signal: list[int] = Field(
        description="Grid signal values (-1=reduce, 0=normal, 1=increase)",
    )
    substitute: bool = Field(
        default=False,
        description="True if this is fallback/substitute data",
    )

    @property
    def quality(self) -> DataQuality:
        """Get data quality based on substitute flag."""
        return DataQuality.SUBSTITUTE if self.substitute else DataQuality.LIVE

    def get_signal_at(self, timestamp: int) -> int | None:
        """Get signal value at a specific unix timestamp."""
        if not self.unix_seconds:
            return None

        for i, ts in enumerate(self.unix_seconds):
            if ts >= timestamp:
                return self.signal[i] if i < len(self.signal) else None
            if i > 0 and self.unix_seconds[i - 1] <= timestamp < ts:
                return self.signal[i - 1]

        if timestamp >= self.unix_seconds[-1]:
            return self.signal[-1] if self.signal else None

        return None


class ProductionType(StrEnum):
    """Power production types from Energy-Charts."""

    NUCLEAR = "nuclear"
    LIGNITE = "lignite"
    COAL = "coal"
    GAS = "gas"
    OIL = "oil"
    HYDRO = "hydro"
    HYDRO_PUMPED = "hydro_pumped"
    WIND_ONSHORE = "wind_onshore"
    WIND_OFFSHORE = "wind_offshore"
    SOLAR = "solar"
    BIOMASS = "biomass"
    GEOTHERMAL = "geothermal"
    OTHER = "other"
    IMPORT = "import"
    EXPORT = "export"


class ProductionData(BaseModel):
    """Power production data for a specific type."""

    production_type: str = Field(description="Type of power production")
    data: list[float | None] = Field(
        description="Power production values in MW (None for missing data)",
    )


class PublicPower(BaseModel):
    """Response model for /public_power endpoint.

    Contains power production by type in MW.
    """

    unix_seconds: list[int] = Field(
        description="Unix timestamps for each data point",
    )
    production_types: list[str] = Field(
        description="List of production type names",
    )
    data: dict[str, list[float | None]] = Field(
        default_factory=dict,
        description="Production data per type in MW",
    )

    @classmethod
    def from_energy_charts_response(cls, response: dict[str, object]) -> "PublicPower":
        """Parse Energy-Charts response into PublicPower model.

        The API returns production types as separate keys with their data arrays.
        """
        unix_seconds = response.get("unix_seconds", [])
        production_types: list[str] = []
        data: dict[str, list[float | None]] = {}

        for key, value in response.items():
            if key == "unix_seconds":
                continue
            if isinstance(value, list) and all(
                isinstance(v, (int, float, type(None))) for v in value
            ):
                production_types.append(key)
                data[key] = value

        return cls(
            unix_seconds=unix_seconds,
            production_types=production_types,
            data=data,
        )

    def get_total_production(self, timestamp: int) -> float | None:
        """Get total power production at a specific timestamp.

        Excludes imports/exports from total.
        """
        if not self.unix_seconds:
            return None

        try:
            idx = self.unix_seconds.index(timestamp)
        except ValueError:
            for i, ts in enumerate(self.unix_seconds):
                if ts >= timestamp:
                    idx = max(0, i - 1)
                    break
            else:
                idx = len(self.unix_seconds) - 1

        total = 0.0
        for prod_type, values in self.data.items():
            if prod_type.lower() in ("import", "export"):
                continue
            value = values[idx] if idx < len(values) else None
            if value is not None:
                total += value

        return total

    def get_renewable_production(self, timestamp: int) -> float | None:
        """Get renewable power production at a specific timestamp."""
        renewable_types = {
            "solar",
            "wind_onshore",
            "wind_offshore",
            "hydro",
            "biomass",
            "geothermal",
        }

        if not self.unix_seconds:
            return None

        try:
            idx = self.unix_seconds.index(timestamp)
        except ValueError:
            for i, ts in enumerate(self.unix_seconds):
                if ts >= timestamp:
                    idx = max(0, i - 1)
                    break
            else:
                idx = len(self.unix_seconds) - 1

        total = 0.0
        for prod_type, values in self.data.items():
            if prod_type.lower() in renewable_types:
                value = values[idx] if idx < len(values) else None
                if value is not None:
                    total += value

        return total


class GridData(BaseModel):
    """Aggregated grid data from multiple endpoints.

    Combines renewable share forecast, grid signal, and production data.
    """

    forecast: RenewableShareForecast | None = Field(
        default=None,
        description="Renewable share forecast data",
    )
    signal: GridSignal | None = Field(
        default=None,
        description="Grid signal data",
    )
    power: PublicPower | None = Field(
        default=None,
        description="Public power production data",
    )
    fetched_at: Annotated[datetime, Field(description="When the data was fetched")]
    country: str = Field(
        default="de",
        description="Country code for the data",
    )

    @property
    def quality(self) -> DataQuality:
        """Get overall data quality.

        Returns the worst quality across all available data sources.
        """
        if self.forecast is None and self.signal is None and self.power is None:
            return DataQuality.UNAVAILABLE

        qualities = []
        if self.forecast is not None:
            qualities.append(self.forecast.quality)
        if self.signal is not None:
            qualities.append(self.signal.quality)

        if DataQuality.SUBSTITUTE in qualities:
            return DataQuality.DEGRADED
        if qualities:
            return DataQuality.LIVE
        return DataQuality.UNAVAILABLE

    @property
    def is_degraded(self) -> bool:
        """Check if data quality is degraded."""
        return self.quality in (DataQuality.SUBSTITUTE, DataQuality.DEGRADED)

    def get_renewable_share_at(self, timestamp: int) -> float | None:
        """Get renewable share at a specific timestamp.

        Uses forecast data if available.
        """
        if self.forecast is not None:
            return self.forecast.get_share_at(timestamp)
        return None
