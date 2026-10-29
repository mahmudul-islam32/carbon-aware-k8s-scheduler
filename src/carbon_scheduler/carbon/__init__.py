"""Carbon intensity estimation module."""

from carbon_scheduler.carbon.accounting import CarbonAccounting, JobEmissions
from carbon_scheduler.carbon.estimator import CarbonEstimator, EmissionFactors
from carbon_scheduler.carbon.live import (
    AvoidedEmissions,
    JobResources,
    estimate_avoided_emissions,
    extract_job_resources,
)
from carbon_scheduler.carbon.models import (
    CarbonIntensityEstimate,
    CounterfactualComparison,
    CumulativeSavings,
    EmissionFactorConfig,
    JobEmissionsEstimate,
    JobEnergyEstimate,
    ProductionMix,
)

__all__ = [
    "AvoidedEmissions",
    "CarbonAccounting",
    "CarbonEstimator",
    "CarbonIntensityEstimate",
    "CounterfactualComparison",
    "CumulativeSavings",
    "EmissionFactorConfig",
    "EmissionFactors",
    "JobEmissions",
    "JobEmissionsEstimate",
    "JobEnergyEstimate",
    "JobResources",
    "ProductionMix",
    "estimate_avoided_emissions",
    "extract_job_resources",
]
