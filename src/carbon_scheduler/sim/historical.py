"""Historical data fetching and storage for backtesting.

Fetches Energy-Charts data for a date range and stores it locally
for reproducible backtesting. Data is stored as JSON files organized
by date.
"""

import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import structlog

from carbon_scheduler.grid.client import DEFAULT_BASE_URL, DEFAULT_TIMEOUT

logger = structlog.get_logger(__name__)


class HistoricalDataPoint:
    """A single historical data point with timestamp and renewable share."""

    def __init__(self, timestamp: datetime, renewable_share_pct: float) -> None:
        self.timestamp = timestamp
        self.renewable_share_pct = renewable_share_pct

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "renewable_share_pct": self.renewable_share_pct,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HistoricalDataPoint":
        """Create from dictionary."""
        return cls(
            timestamp=datetime.fromisoformat(data["timestamp"]),
            renewable_share_pct=float(data["renewable_share_pct"]),
        )


class HistoricalData:
    """Historical grid data for a date range."""

    def __init__(self, points: list[HistoricalDataPoint]) -> None:
        self.points = sorted(points, key=lambda p: p.timestamp)

    @property
    def start_time(self) -> datetime | None:
        """Get the start timestamp of the data."""
        return self.points[0].timestamp if self.points else None

    @property
    def end_time(self) -> datetime | None:
        """Get the end timestamp of the data."""
        return self.points[-1].timestamp if self.points else None

    def get_renewable_share_at(self, timestamp: datetime) -> float | None:
        """Get the renewable share at a specific timestamp.

        Uses linear interpolation between adjacent points.
        """
        if not self.points:
            return None

        if timestamp <= self.points[0].timestamp:
            return self.points[0].renewable_share_pct
        if timestamp >= self.points[-1].timestamp:
            return self.points[-1].renewable_share_pct

        for i in range(len(self.points) - 1):
            before = self.points[i]
            after = self.points[i + 1]

            if before.timestamp <= timestamp <= after.timestamp:
                time_delta = (after.timestamp - before.timestamp).total_seconds()
                if time_delta == 0:
                    return before.renewable_share_pct

                weight = (timestamp - before.timestamp).total_seconds() / time_delta
                return before.renewable_share_pct * (1 - weight) + after.renewable_share_pct * weight

        return self.points[-1].renewable_share_pct

    def get_average_renewable_share(
        self,
        start: datetime,
        end: datetime,
        sample_interval: timedelta = timedelta(minutes=15),
    ) -> float:
        """Get the average renewable share over a time window."""
        if not self.points or end <= start:
            return 0.0

        samples: list[float] = []
        current = start
        while current < end:
            share = self.get_renewable_share_at(current)
            if share is not None:
                samples.append(share)
            current += sample_interval

        return sum(samples) / len(samples) if samples else 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "points": [p.to_dict() for p in self.points],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HistoricalData":
        """Create from dictionary."""
        return cls(
            points=[HistoricalDataPoint.from_dict(p) for p in data.get("points", [])],
        )


class HistoricalDataFetcher:
    """Fetches historical data from Energy-Charts API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        country: str = "de",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.country = country

    async def fetch_day(self, target_date: date) -> list[HistoricalDataPoint]:
        """Fetch historical data for a single day.

        Args:
            target_date: The date to fetch data for.

        Returns:
            List of HistoricalDataPoint for the day.
        """
        endpoint = "/ren_share"
        start_dt = datetime(target_date.year, target_date.month, target_date.day)
        end_dt = start_dt + timedelta(days=1)

        params = {
            "country": self.country,
            "start": start_dt.strftime("%Y-%m-%dT%H:%M"),
            "end": end_dt.strftime("%Y-%m-%dT%H:%M"),
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.get(f"{self.base_url}{endpoint}", params=params)
                response.raise_for_status()
                data = response.json()

                points: list[HistoricalDataPoint] = []
                unix_seconds = data.get("unix_seconds", [])
                ren_shares = data.get("ren_share", [])

                for ts, share in zip(unix_seconds, ren_shares, strict=False):
                    if share is not None:
                        points.append(
                            HistoricalDataPoint(
                                timestamp=datetime.fromtimestamp(ts),
                                renewable_share_pct=float(share),
                            )
                        )

                logger.info(
                    "fetched_historical_day",
                    date=target_date.isoformat(),
                    points=len(points),
                )
                return points

            except httpx.HTTPStatusError as e:
                logger.error(
                    "fetch_day_error",
                    date=target_date.isoformat(),
                    status=e.response.status_code,
                )
                return []
            except Exception as e:
                logger.error(
                    "fetch_day_error",
                    date=target_date.isoformat(),
                    error=str(e),
                )
                return []

    async def fetch_range(
        self,
        start_date: date,
        end_date: date,
        progress_callback: Any | None = None,
    ) -> HistoricalData:
        """Fetch historical data for a date range.

        Args:
            start_date: Start date (inclusive).
            end_date: End date (inclusive).
            progress_callback: Optional callback for progress updates.

        Returns:
            HistoricalData containing all fetched points.
        """
        all_points: list[HistoricalDataPoint] = []
        current_date = start_date
        total_days = (end_date - start_date).days + 1
        fetched_days = 0

        while current_date <= end_date:
            points = await self.fetch_day(current_date)
            all_points.extend(points)
            fetched_days += 1

            if progress_callback:
                progress_callback(fetched_days, total_days)

            current_date += timedelta(days=1)
            await asyncio.sleep(0.1)

        logger.info(
            "fetched_historical_range",
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            total_points=len(all_points),
        )

        return HistoricalData(points=all_points)


class HistoricalDataStore:
    """Stores and loads historical data from local files."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _get_metadata_path(self) -> Path:
        """Get path to metadata file."""
        return self.data_dir / "metadata.json"

    def _get_data_path(self) -> Path:
        """Get path to data file."""
        return self.data_dir / "historical_data.json"

    def save(self, data: HistoricalData, start_date: date, end_date: date) -> None:
        """Save historical data to disk.

        Args:
            data: The historical data to save.
            start_date: The start date of the data range.
            end_date: The end date of the data range.
        """
        metadata = {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "num_points": len(data.points),
            "country": "de",
            "created_at": datetime.now().isoformat(),
        }

        with open(self._get_metadata_path(), "w") as f:
            json.dump(metadata, f, indent=2)

        with open(self._get_data_path(), "w") as f:
            json.dump(data.to_dict(), f)

        logger.info(
            "saved_historical_data",
            path=str(self.data_dir),
            points=len(data.points),
        )

    def load(self) -> HistoricalData | None:
        """Load historical data from disk.

        Returns:
            HistoricalData if data exists, None otherwise.
        """
        data_path = self._get_data_path()
        if not data_path.exists():
            return None

        with open(data_path) as f:
            data_dict = json.load(f)

        data = HistoricalData.from_dict(data_dict)
        logger.info(
            "loaded_historical_data",
            path=str(self.data_dir),
            points=len(data.points),
        )
        return data

    def get_metadata(self) -> dict[str, Any] | None:
        """Get metadata about stored data.

        Returns:
            Metadata dictionary or None if no data stored.
        """
        metadata_path = self._get_metadata_path()
        if not metadata_path.exists():
            return None

        with open(metadata_path) as f:
            result: dict[str, Any] = json.load(f)
            return result

    def has_data(self) -> bool:
        """Check if data has been stored."""
        return self._get_data_path().exists()

    def covers_range(self, start_date: date, end_date: date) -> bool:
        """Check if stored data covers the requested range.

        Args:
            start_date: Requested start date.
            end_date: Requested end date.

        Returns:
            True if stored data covers the entire range.
        """
        metadata = self.get_metadata()
        if not metadata:
            return False

        stored_start = date.fromisoformat(metadata["start_date"])
        stored_end = date.fromisoformat(metadata["end_date"])

        return stored_start <= start_date and stored_end >= end_date


__all__ = [
    "HistoricalData",
    "HistoricalDataFetcher",
    "HistoricalDataPoint",
    "HistoricalDataStore",
]
