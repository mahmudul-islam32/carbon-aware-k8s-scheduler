"""Configuration settings for the carbon scheduler."""

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="CARBON_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Grid data settings
    grid_country: str = Field(
        default="de",
        description="Energy-Charts country code",
    )
    grid_api_base_url: str = Field(
        default="https://api.energy-charts.info",
        description="Base URL for Energy-Charts API",
    )
    forecast_refresh_minutes: int = Field(
        default=30,
        ge=5,
        le=120,
        description="How often to refresh forecast data (minutes)",
    )

    # Policy settings
    policy: Literal["immediate", "threshold", "greenest-window", "balanced"] = Field(
        default="greenest-window",
        description="Window selection policy",
    )
    renewable_threshold_pct: int = Field(
        default=60,
        ge=0,
        le=100,
        description="Threshold policy: release when forecast share >= this value",
    )
    delay_penalty_pct_per_hour: float = Field(
        default=2.0,
        ge=0,
        description="Balanced policy: renewable-share points subtracted per hour of delay",
    )
    max_releases_per_minute: int = Field(
        default=0,
        ge=0,
        description="Release staggering: max planned releases per minute (0 disables)",
    )
    dashboard_enabled: bool = Field(
        default=True,
        description="Serve the web dashboard and /api/state on the metrics port",
    )
    max_delay: str = Field(
        default="24h",
        description="Maximum time a job may be held after submission (e.g. '24h')",
    )
    replan_enabled: bool = Field(
        default=True,
        description="Re-plan held jobs when a newer forecast is fetched",
    )
    replan_min_improvement_pct: float = Field(
        default=3.0,
        ge=0,
        le=100,
        description="Hysteresis: move a plan only if the new window is greener by more "
        "than this many percentage points",
    )

    # KEDA ScaledJob mode (F-11)
    keda_enabled: bool = Field(
        default=False,
        description="Serve /keda/allowed-concurrency for the KEDA metrics-api scaler and keep "
        "the forecast fresh even when no Jobs are held",
    )
    keda_min_concurrency: int = Field(
        default=0,
        ge=0,
        description="KEDA mode: allowed concurrency at or below keda_low_share_pct",
    )
    keda_max_concurrency: int = Field(
        default=10,
        ge=0,
        description="KEDA mode: allowed concurrency at or above keda_high_share_pct, and the "
        "fail-open value when grid data is missing, degraded or stale",
    )
    keda_low_share_pct: float = Field(
        default=40.0,
        ge=0,
        le=100,
        description="KEDA mode: renewable share (%) at or below which only the minimum runs",
    )
    keda_high_share_pct: float = Field(
        default=80.0,
        ge=0,
        le=100,
        description="KEDA mode: renewable share (%) at or above which the maximum runs",
    )

    @model_validator(mode="after")
    def _check_keda_bounds(self) -> "Settings":
        """Reject inverted KEDA ranges early, at start-up."""
        if self.keda_min_concurrency > self.keda_max_concurrency:
            raise ValueError("keda_min_concurrency must be <= keda_max_concurrency")
        if self.keda_low_share_pct >= self.keda_high_share_pct:
            raise ValueError("keda_low_share_pct must be < keda_high_share_pct")
        return self

    # Live CO2 accounting
    fossil_intensity_gco2_kwh: float = Field(
        default=400.0,
        ge=0,
        description="Carbon intensity attributed to the non-renewable share (gCO2/kWh)",
    )
    watts_per_cpu_core: float = Field(
        default=10.0,
        ge=0,
        description="Estimated power draw per requested CPU core (W)",
    )
    watts_per_gpu: float = Field(
        default=200.0,
        ge=0,
        description="Estimated power draw per requested GPU (W)",
    )

    # Emission factors
    emission_factors_file: Path = Field(
        default=Path("config/emission_factors.yaml"),
        description="Path to emission factors YAML file",
    )

    # Safety margins
    deadline_safety_margin_minutes: int = Field(
        default=5,
        ge=0,
        description="Safety margin before deadline (minutes)",
    )

    # Metrics
    metrics_port: int = Field(
        default=9090,
        ge=1,
        le=65535,
        description="Prometheus metrics port",
    )

    # Controller settings
    reconcile_interval_seconds: int = Field(
        default=60,
        ge=10,
        le=3600,
        description="How often to reconcile job states (seconds)",
    )
    leader_election: bool = Field(
        default=True,
        description="Enable leader election for HA deployments",
    )

    # Logging
    log_level: str = Field(
        default="INFO",
        description="Logging level",
    )
    log_format: Literal["json", "console"] = Field(
        default="json",
        description="Log output format",
    )


# Annotation keys
ANNOTATION_PREFIX = "carbon-scheduler.mahmudul-islam32.github.io"
ANNOTATION_FLEXIBLE = f"{ANNOTATION_PREFIX}/flexible"
ANNOTATION_DEADLINE = f"{ANNOTATION_PREFIX}/deadline"
# Relative deadline (e.g. "6h"), resolved against the Job's creation time. Handy for CronJobs.
ANNOTATION_DEADLINE_IN = f"{ANNOTATION_PREFIX}/deadline-in"
ANNOTATION_ESTIMATED_RUNTIME = f"{ANNOTATION_PREFIX}/estimated-runtime"
ANNOTATION_PLANNED_START = f"{ANNOTATION_PREFIX}/planned-start"
ANNOTATION_ACTUAL_START = f"{ANNOTATION_PREFIX}/actual-start"
ANNOTATION_CO2_AVOIDED = f"{ANNOTATION_PREFIX}/co2-avoided-grams"
ANNOTATION_POLICY_USED = f"{ANNOTATION_PREFIX}/policy-used"
ANNOTATION_FALLBACK_REASON = f"{ANNOTATION_PREFIX}/fallback-reason"
# Optional per-job overrides
ANNOTATION_POLICY = f"{ANNOTATION_PREFIX}/policy"
ANNOTATION_RENEWABLE_THRESHOLD = f"{ANNOTATION_PREFIX}/renewable-threshold"
ANNOTATION_MAX_DELAY = f"{ANNOTATION_PREFIX}/max-delay"
ANNOTATION_DELAY_PENALTY = f"{ANNOTATION_PREFIX}/delay-penalty"
# Written by the controller
ANNOTATION_LATEST_START = f"{ANNOTATION_PREFIX}/latest-start"
ANNOTATION_EXPECTED_RENEWABLE_SHARE = f"{ANNOTATION_PREFIX}/expected-renewable-share"
# Re-planning (F-20): when the current plan was made or last checked against a forecast
ANNOTATION_PLANNED_AT = f"{ANNOTATION_PREFIX}/planned-at"


def get_settings() -> Settings:
    """Get application settings singleton."""
    return Settings()
