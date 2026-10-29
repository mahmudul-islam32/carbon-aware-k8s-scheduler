"""Simulation and backtest CLI module."""

from carbon_scheduler.sim.backtest import Backtest, BacktestResult, JobResult, PolicyResult
from carbon_scheduler.sim.historical import (
    HistoricalData,
    HistoricalDataFetcher,
    HistoricalDataPoint,
    HistoricalDataStore,
)
from carbon_scheduler.sim.plan import PlanResult, plan_job
from carbon_scheduler.sim.trace import (
    JobTrace,
    SyntheticTraceGenerator,
    TraceJob,
    parse_slack_range,
)

__all__ = [
    "Backtest",
    "BacktestResult",
    "HistoricalData",
    "HistoricalDataFetcher",
    "HistoricalDataPoint",
    "HistoricalDataStore",
    "JobResult",
    "JobTrace",
    "PlanResult",
    "PolicyResult",
    "SyntheticTraceGenerator",
    "TraceJob",
    "parse_slack_range",
    "plan_job",
]
