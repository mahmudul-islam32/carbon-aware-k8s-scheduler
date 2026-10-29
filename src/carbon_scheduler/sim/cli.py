"""CLI entry point for the carbon scheduler simulation and backtest tool.

Provides commands for:
- Fetching historical grid data
- Generating synthetic job traces
- Running backtests comparing policies
- Displaying result summaries
"""

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from carbon_scheduler.planner.models import PolicyConfig
from carbon_scheduler.planner.policies import PolicyName
from carbon_scheduler.sim.backtest import Backtest, BacktestResult
from carbon_scheduler.sim.historical import (
    HistoricalDataFetcher,
    HistoricalDataStore,
)
from carbon_scheduler.sim.trace import (
    JobTrace,
    SyntheticTraceGenerator,
    parse_slack_range,
)

console = Console()

VALID_POLICIES: list[PolicyName] = ["immediate", "threshold", "greenest-window"]


def parse_date(
    _ctx: click.Context,
    _param: click.Parameter,
    value: str | None,
) -> date | None:
    """Parse a date string in YYYY-MM-DD format."""
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise click.BadParameter(f"Invalid date format: {value}. Use YYYY-MM-DD.") from e


def parse_policies(
    _ctx: click.Context,
    _param: click.Parameter,
    value: str,
) -> list[PolicyName]:
    """Parse a comma-separated list of policy names."""
    policies: list[PolicyName] = []
    for name in value.split(","):
        policy_name = name.strip().lower()
        if policy_name not in VALID_POLICIES:
            raise click.BadParameter(
                f"Unknown policy: {policy_name}. Valid policies: {', '.join(VALID_POLICIES)}"
            )
        if policy_name == "immediate":
            policies.append("immediate")
        elif policy_name == "threshold":
            policies.append("threshold")
        elif policy_name == "greenest-window":
            policies.append("greenest-window")
    return policies


@click.group()
@click.version_option(version="0.1.0", prog_name="carbon-sim")
def main() -> None:
    """Carbon-aware scheduler simulation and backtest tool.

    Use this tool to:

    \b
    1. Fetch historical grid data for a date range
    2. Generate synthetic job traces
    3. Run backtests comparing different scheduling policies
    4. View summary reports

    Example workflow:

    \b
      # Fetch 3 months of historical data
      carbon-sim fetch --start 2026-07-01 --end 2026-09-30 --output data/

    \b
      # Generate a synthetic job trace
      carbon-sim generate-trace --jobs 100 --slack 1h-6h --output trace.json

    \b
      # Run backtest comparing policies
      carbon-sim backtest --data data/ --trace trace.json \\
        --policies immediate,threshold,greenest-window --output results.csv

    \b
      # View summary
      carbon-sim summary results.csv
    """
    pass


@main.command("fetch")
@click.option(
    "--start",
    required=True,
    callback=parse_date,
    help="Start date (YYYY-MM-DD)",
)
@click.option(
    "--end",
    required=True,
    callback=parse_date,
    help="End date (YYYY-MM-DD)",
)
@click.option(
    "--output",
    "-o",
    required=True,
    type=click.Path(path_type=Path),
    help="Output directory for historical data",
)
@click.option(
    "--country",
    default="de",
    help="Country code (default: de for Germany)",
)
def fetch_command(start: date, end: date, output: Path, country: str) -> None:
    """Fetch historical grid data for a date range.

    Downloads renewable share data from Energy-Charts API and stores
    it locally for backtesting. Data is fetched once and reused.
    """
    if start > end:
        raise click.BadParameter("Start date must be before end date")

    store = HistoricalDataStore(output)

    if store.covers_range(start, end):
        console.print(f"[green]Data already exists for {start} to {end}[/green]")
        metadata = store.get_metadata()
        if metadata:
            console.print(f"  Points: {metadata.get('num_points', 'unknown')}")
            console.print(f"  Created: {metadata.get('created_at', 'unknown')}")
        return

    console.print(f"[blue]Fetching historical data from {start} to {end}...[/blue]")

    fetcher = HistoricalDataFetcher(country=country)

    async def _fetch() -> None:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task(
                "Fetching data...",
                total=None,
            )

            def progress_callback(done: int, total: int) -> None:
                progress.update(task, description=f"Fetching day {done}/{total}...")

            data = await fetcher.fetch_range(start, end, progress_callback)

            progress.update(task, description="Saving data...")
            store.save(data, start, end)

    asyncio.run(_fetch())

    console.print(f"[green]Done! Data saved to {output}[/green]")
    metadata = store.get_metadata()
    if metadata:
        console.print(f"  Points: {metadata.get('num_points', 'unknown')}")


@main.command("generate-trace")
@click.option(
    "--jobs",
    "-n",
    required=True,
    type=int,
    help="Number of jobs to generate",
)
@click.option(
    "--slack",
    default="1h-6h",
    help="Slack time range (e.g., '1h-6h', '30m-2h')",
)
@click.option(
    "--runtime",
    default="30m-2h",
    help="Runtime range (e.g., '30m-2h', '1h-4h')",
)
@click.option(
    "--start",
    callback=parse_date,
    default=None,
    help="Start date for job submissions (default: 7 days ago)",
)
@click.option(
    "--end",
    callback=parse_date,
    default=None,
    help="End date for job deadlines (default: today)",
)
@click.option(
    "--seed",
    type=int,
    default=None,
    help="Random seed for reproducibility",
)
@click.option(
    "--output",
    "-o",
    required=True,
    type=click.Path(path_type=Path),
    help="Output file for trace (JSON)",
)
def generate_trace_command(
    jobs: int,
    slack: str,
    runtime: str,
    start: date | None,
    end: date | None,
    seed: int | None,
    output: Path,
) -> None:
    """Generate a synthetic job trace for backtesting.

    Creates a JSON file with randomized jobs distributed across the
    specified time range. Use --seed for deterministic generation.
    """
    if start is None:
        start = date.today() - timedelta(days=7)
    if end is None:
        end = date.today()

    if start > end:
        raise click.BadParameter("Start date must be before end date")

    try:
        slack_min, slack_max = parse_slack_range(slack)
    except ValueError as e:
        raise click.BadParameter(str(e)) from e

    try:
        runtime_min, runtime_max = parse_slack_range(runtime)
    except ValueError as e:
        raise click.BadParameter(str(e)) from e

    start_time = datetime(start.year, start.month, start.day)
    end_time = datetime(end.year, end.month, end.day, 23, 59, 59)

    generator = SyntheticTraceGenerator(seed=seed)
    trace = generator.generate(
        num_jobs=jobs,
        start_time=start_time,
        end_time=end_time,
        slack_min=slack_min,
        slack_max=slack_max,
        runtime_min=runtime_min,
        runtime_max=runtime_max,
    )

    trace.save(output)

    console.print(f"[green]Generated {jobs} jobs[/green]")
    console.print(f"  Time range: {start} to {end}")
    console.print(f"  Slack: {slack}")
    console.print(f"  Runtime: {runtime}")
    if seed is not None:
        console.print(f"  Seed: {seed}")
    console.print(f"  Output: {output}")


@main.command("backtest")
@click.option(
    "--data",
    "-d",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Directory containing historical data",
)
@click.option(
    "--trace",
    "-t",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Job trace file (JSON)",
)
@click.option(
    "--policies",
    "-p",
    required=True,
    callback=parse_policies,
    help="Comma-separated list of policies to compare",
)
@click.option(
    "--output",
    "-o",
    required=True,
    type=click.Path(path_type=Path),
    help="Output file for results (CSV)",
)
@click.option(
    "--threshold",
    type=int,
    default=60,
    help="Renewable threshold for threshold policy (0-100, default: 60)",
)
@click.option(
    "--safety-margin",
    type=int,
    default=5,
    help="Safety margin in minutes before deadline (default: 5)",
)
def backtest_command(
    data: Path,
    trace: Path,
    policies: list[PolicyName],
    output: Path,
    threshold: int,
    safety_margin: int,
) -> None:
    """Run backtest comparing scheduling policies.

    Simulates each policy against the same job trace using historical
    grid data. Outputs detailed results to CSV and prints a summary.
    """
    store = HistoricalDataStore(data)
    historical_data = store.load()

    if historical_data is None:
        raise click.ClickException(f"No historical data found in {data}. Run 'fetch' first.")

    job_trace = JobTrace.load(trace)

    if len(job_trace) == 0:
        raise click.ClickException(f"Job trace is empty: {trace}")

    config = PolicyConfig(
        renewable_threshold_pct=threshold,
        safety_margin=timedelta(minutes=safety_margin),
    )

    console.print(f"[blue]Running backtest with {len(job_trace)} jobs...[/blue]")
    console.print(f"  Policies: {', '.join(policies)}")
    console.print(f"  Threshold: {threshold}%")
    console.print(f"  Safety margin: {safety_margin} minutes")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Running backtest...", total=None)

        backtest = Backtest(historical_data, config=config)
        result = backtest.run(job_trace, policies)

        progress.update(task, description="Saving results...")
        result.save_csv(output)

    console.print(f"\n[green]Results saved to {output}[/green]\n")

    _print_summary_table(result)


@main.command("summary")
@click.argument("results_file", type=click.Path(exists=True, path_type=Path))
def summary_command(results_file: Path) -> None:
    """Display summary of backtest results.

    Reads a CSV results file and displays a formatted summary table
    with key metrics for each policy.
    """
    result = BacktestResult.load_csv(results_file)

    if not result.policy_results:
        raise click.ClickException("No results found in file")

    console.print("\n[bold]Backtest Results Summary[/bold]")
    console.print(f"Source: {results_file}\n")

    _print_summary_table(result)


def _print_summary_table(result: BacktestResult) -> None:
    """Print a rich summary table for backtest results."""
    table = Table(title="Policy Comparison", show_header=True, header_style="bold cyan")

    table.add_column("Policy", style="bold")
    table.add_column("Total Jobs", justify="right")
    table.add_column("Deadline Misses", justify="right")
    table.add_column("Miss Rate", justify="right")
    table.add_column("Mean Delay", justify="right")
    table.add_column("Mean Renewable %", justify="right")
    table.add_column("Total CO₂ (kg)", justify="right")
    table.add_column("Avoided CO₂ (kg)", justify="right")
    table.add_column("Savings %", justify="right")
    table.add_column("Fallbacks", justify="right")

    for policy_name, policy_result in result.policy_results.items():
        avoided_co2 = result.get_avoided_co2(policy_name)
        avoided_pct = result.get_avoided_co2_pct(policy_name)

        miss_style = "red" if policy_result.deadline_misses > 0 else "green"
        avoided_style = "green" if avoided_co2 > 0 else ("red" if avoided_co2 < 0 else "")

        table.add_row(
            policy_name,
            str(policy_result.total_jobs),
            f"[{miss_style}]{policy_result.deadline_misses}[/{miss_style}]",
            f"[{miss_style}]{policy_result.deadline_miss_rate:.1%}[/{miss_style}]",
            _format_duration(policy_result.mean_delay_seconds),
            f"{policy_result.mean_renewable_share:.1f}%",
            f"{policy_result.total_co2_grams / 1000:.2f}",
            f"[{avoided_style}]{avoided_co2 / 1000:.2f}[/{avoided_style}]" if policy_name != "immediate" else "-",
            f"[{avoided_style}]{avoided_pct:.1f}%[/{avoided_style}]" if policy_name != "immediate" else "-",
            str(policy_result.fallback_count),
        )

    console.print(table)

    if "immediate" in result.policy_results:
        best_policy = None
        best_savings = 0.0
        for policy_name in result.policy_results:
            if policy_name == "immediate":
                continue
            savings = result.get_avoided_co2(policy_name)
            policy_result = result.policy_results[policy_name]
            if savings > best_savings and policy_result.deadline_misses == 0:
                best_savings = savings
                best_policy = policy_name

        if best_policy:
            console.print(f"\n[bold green]Best policy: {best_policy}[/bold green]")
            console.print(f"  Avoided {best_savings / 1000:.2f} kg CO₂ ({result.get_avoided_co2_pct(best_policy):.1f}%) vs immediate")
            console.print("  Zero deadline misses ✓")


def _format_duration(seconds: float) -> str:
    """Format a duration in seconds to human-readable string."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        return f"{seconds / 60:.1f}m"
    else:
        hours = seconds / 3600
        return f"{hours:.1f}h"


if __name__ == "__main__":
    main()
