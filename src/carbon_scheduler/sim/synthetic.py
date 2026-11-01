"""Synthetic, realistic-looking renewable-share curves.

Used by the dashboard demo mode and by ``carbon-sim synth`` so every tool in
this project can be tried fully offline. The curve combines:

* a **solar** bell curve between sunrise and sunset whose height varies per day
  (cloudiness) and per season,
* a slowly drifting **wind** component (smoothed random walk, multi-day weather
  fronts),
* a small constant **base** (hydro and biomass),
* a little high-frequency noise.

The output is clipped to 3-97 % and is deterministic for a given seed.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

from carbon_scheduler.planner.models import Forecast, ForecastPoint


def _solar_profile(hour: float, day_of_year: int) -> float:
    """Clear-sky solar contribution (0-1) for a fractional local hour."""
    # Longer, higher days in summer (Germany ~ 51 deg N)
    season = math.cos(2 * math.pi * (day_of_year - 172) / 365)  # 1 at midsummer
    half_day = 6.0 + 2.2 * season  # hours from solar noon to sunset
    noon = 13.0
    x = (hour - noon) / half_day
    if abs(x) >= 1:
        return 0.0
    peak = 0.75 + 0.25 * season
    return peak * math.cos(x * math.pi / 2) ** 2


def synthetic_shares(
    start: datetime,
    hours: float,
    step_minutes: int = 15,
    seed: int | None = 42,
    tz_offset_hours: float = 1.0,
) -> list[tuple[datetime, float]]:
    """Generate ``(timestamp, renewable_share_pct)`` pairs.

    Args:
        start: First timestamp (timezone-aware; naive is treated as UTC).
        hours: Length of the series.
        step_minutes: Spacing between points.
        seed: Random seed; the same seed gives the same curve.
        tz_offset_hours: Local time offset used for the solar profile (CET = 1).

    Returns:
        A list of points in chronological order.
    """
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    rng = random.Random(seed)
    steps = max(1, int(hours * 60 / step_minutes) + 1)

    wind = rng.uniform(25, 45)
    wind_target = rng.uniform(15, 60)
    cloud_by_day: dict[int, float] = {}
    points: list[tuple[datetime, float]] = []

    for i in range(steps):
        ts = start + timedelta(minutes=i * step_minutes)
        local = ts + timedelta(hours=tz_offset_hours)
        day_key = local.toordinal()
        if day_key not in cloud_by_day:
            cloud_by_day[day_key] = rng.uniform(0.35, 1.0)

        # Weather fronts: pick a new wind target every ~18 hours on average
        if rng.random() < step_minutes / (18 * 60):
            wind_target = rng.uniform(10, 65)
        wind += (wind_target - wind) * 0.02 * (step_minutes / 15) + rng.gauss(0, 0.8)
        wind = min(70.0, max(5.0, wind))

        hour = local.hour + local.minute / 60
        solar = 55 * cloud_by_day[day_key] * _solar_profile(hour, local.timetuple().tm_yday)
        share = 8 + wind + solar + rng.gauss(0, 1.2)
        points.append((ts, round(min(97.0, max(3.0, share)), 1)))

    return points


def synthetic_forecast(
    start: datetime,
    hours: float = 48,
    step_minutes: int = 15,
    seed: int | None = 42,
) -> Forecast:
    """Synthetic data wrapped as a planner :class:`Forecast`."""
    return Forecast(
        points=[
            ForecastPoint(timestamp=ts, renewable_share_pct=share)
            for ts, share in synthetic_shares(start, hours, step_minutes, seed)
        ]
    )
