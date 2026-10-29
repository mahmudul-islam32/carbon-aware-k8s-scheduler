"""Pydantic models for carbon intensity estimation and accounting."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field


class EmissionFactorConfig(BaseModel):
    """Configuration for emission factors loaded from YAML."""

    version: str
    country: str
    unit: str = "gCO2/kWh"
    emission_factors: dict[str, float]
    default_factor: float = 500.0


class ProductionMix(BaseModel):
    """Power production mix at a point in time.

    Keys are production types (e.g., 'Wind Onshore', 'Solar', 'Lignite')
    and values are power output in MW.
    """

    timestamp: datetime
    production: dict[str, float] = Field(
        description="Production per type in MW",
    )


class CarbonIntensityEstimate(BaseModel):
    """Estimated carbon intensity for a time interval.

    All values are ESTIMATES based on production mix and emission factors.
    """

    timestamp: datetime
    carbon_intensity_gco2_kwh: Annotated[
        float,
        Field(description="Estimated carbon intensity in gCO2/kWh"),
    ]
    is_estimate: bool = Field(
        default=True,
        description="Flag indicating this is an estimate, not a measurement",
    )
    production_types_used: list[str] = Field(
        default_factory=list,
        description="Production types included in calculation",
    )
    missing_types_defaulted: list[str] = Field(
        default_factory=list,
        description="Production types that used the default emission factor",
    )


class JobEnergyEstimate(BaseModel):
    """Estimated energy consumption for a job.

    Energy = requested resources * power per unit * runtime.
    All values are ESTIMATES.
    """

    job_name: str
    job_namespace: str
    cpu_cores: float = Field(ge=0, description="Requested CPU cores")
    gpu_units: float = Field(ge=0, default=0, description="Requested GPU units")
    runtime_seconds: float = Field(ge=0, description="Job runtime in seconds")
    estimated_energy_kwh: Annotated[
        float,
        Field(
            ge=0,
            description="Estimated energy consumption in kWh (ESTIMATE)",
        ),
    ]
    is_estimate: bool = Field(
        default=True,
        description="Flag indicating this is an estimate, not a measurement",
    )


class JobEmissionsEstimate(BaseModel):
    """Estimated CO2 emissions for a job.

    Emissions = energy * carbon intensity.
    All values are ESTIMATES.
    """

    job_name: str
    job_namespace: str
    start_time: datetime
    end_time: datetime
    estimated_energy_kwh: float = Field(ge=0)
    estimated_carbon_intensity_gco2_kwh: float = Field(ge=0)
    estimated_emissions_gco2: Annotated[
        float,
        Field(
            ge=0,
            description="Estimated CO2 emissions in grams (ESTIMATE)",
        ),
    ]
    is_estimate: bool = Field(
        default=True,
        description="Flag indicating this is an estimate, not a measurement",
    )


class CounterfactualComparison(BaseModel):
    """Comparison of actual vs counterfactual (immediate start) emissions.

    Avoided CO2 = counterfactual emissions - actual emissions.
    Positive values indicate carbon savings from delayed start.
    All values are ESTIMATES.
    """

    job_name: str
    job_namespace: str
    submit_time: datetime
    actual_start_time: datetime
    runtime_seconds: float
    actual_emissions_gco2: Annotated[
        float,
        Field(description="Estimated emissions at actual start time (ESTIMATE)"),
    ]
    counterfactual_emissions_gco2: Annotated[
        float,
        Field(description="Estimated emissions if job started at submit time (ESTIMATE)"),
    ]
    avoided_emissions_gco2: Annotated[
        float,
        Field(description="Estimated avoided CO2 (counterfactual - actual) (ESTIMATE)"),
    ]
    savings_percent: Annotated[
        float,
        Field(description="Percentage of emissions saved (ESTIMATE)"),
    ]
    is_estimate: bool = Field(
        default=True,
        description="Flag indicating all values are estimates, not measurements",
    )


class CumulativeSavings(BaseModel):
    """Cumulative CO2 savings across all tracked jobs.

    All values are ESTIMATES.
    """

    total_jobs: int = Field(ge=0)
    total_avoided_gco2: float = Field(
        description="Total estimated avoided CO2 in grams (ESTIMATE)",
    )
    total_avoided_kg_co2: float = Field(
        description="Total estimated avoided CO2 in kg (ESTIMATE)",
    )
    total_actual_emissions_gco2: float = Field(
        ge=0,
        description="Total actual emissions in grams (ESTIMATE)",
    )
    total_counterfactual_emissions_gco2: float = Field(
        ge=0,
        description="Total counterfactual emissions in grams (ESTIMATE)",
    )
    average_savings_percent: float = Field(
        description="Average savings percentage across jobs (ESTIMATE)",
    )
    is_estimate: bool = Field(
        default=True,
        description="Flag indicating all values are estimates, not measurements",
    )
