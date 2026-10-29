"""Configuration settings for the carbon scheduler."""

from pathlib import Path
from typing import Literal

from pydantic import Field
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
    policy: Literal["immediate", "threshold", "greenest-window"] = Field(
        default="greenest-window",
        description="Window selection policy",
    )
    renewable_threshold_pct: int = Field(
        default=60,
        ge=0,
        le=100,
        description="Threshold policy: release when forecast share >= this value",
    )
    max_delay: str = Field(
        default="24h",
        description="Maximum time a job may be held after submission (e.g. '24h')",
    )

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
ANNOTATION_ESTIMATED_RUNTIME = f"{ANNOTATION_PREFIX}/estimated-runtime"
ANNOTATION_PLANNED_START = f"{ANNOTATION_PREFIX}/planned-start"
ANNOTATION_ACTUAL_START = f"{ANNOTATION_PREFIX}/actual-start"
ANNOTATION_CO2_AVOIDED = f"{ANNOTATION_PREFIX}/co2-avoided-grams"
ANNOTATION_POLICY_USED = f"{ANNOTATION_PREFIX}/policy-used"
ANNOTATION_FALLBACK_REASON = f"{ANNOTATION_PREFIX}/fallback-reason"
# Relative deadline (F-22): resolved against the Job's creationTimestamp
ANNOTATION_DEADLINE_IN = f"{ANNOTATION_PREFIX}/deadline-in"
# Optional per-job overrides
ANNOTATION_POLICY = f"{ANNOTATION_PREFIX}/policy"
ANNOTATION_RENEWABLE_THRESHOLD = f"{ANNOTATION_PREFIX}/renewable-threshold"
ANNOTATION_MAX_DELAY = f"{ANNOTATION_PREFIX}/max-delay"
# Written by the controller
ANNOTATION_LATEST_START = f"{ANNOTATION_PREFIX}/latest-start"
ANNOTATION_EXPECTED_RENEWABLE_SHARE = f"{ANNOTATION_PREFIX}/expected-renewable-share"
# Marks a `deadline` the controller resolved from `deadline-in`
ANNOTATION_DEADLINE_SOURCE = f"{ANNOTATION_PREFIX}/deadline-source"
DEADLINE_SOURCE_RELATIVE = "deadline-in"


def get_settings() -> Settings:
    """Get application settings singleton."""
    return Settings()
