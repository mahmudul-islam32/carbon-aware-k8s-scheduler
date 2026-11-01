"""``kubectl carbon``: inspect and steer carbon-aware Jobs from the terminal (F-29).

Installed as the ``kubectl-carbon`` executable, so kubectl picks it up as a
plugin::

    kubectl carbon status -A              # every flexible Job and its plan
    kubectl carbon explain nightly-etl    # the full decision for one Job
    kubectl carbon release nightly-etl    # release a held Job right now
    kubectl carbon forecast               # renewable share for the next hours

All cluster access goes through the official Kubernetes Python client and your
current kubeconfig context.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from carbon_scheduler.config import (
    ANNOTATION_ACTUAL_START,
    ANNOTATION_CO2_AVOIDED,
    ANNOTATION_DEADLINE,
    ANNOTATION_ESTIMATED_RUNTIME,
    ANNOTATION_EXPECTED_RENEWABLE_SHARE,
    ANNOTATION_FALLBACK_REASON,
    ANNOTATION_FLEXIBLE,
    ANNOTATION_LATEST_START,
    ANNOTATION_PLANNED_START,
    ANNOTATION_POLICY_USED,
    ANNOTATION_PREFIX,
)

console = Console()


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _humanize(delta_seconds: float) -> str:
    sign = "-" if delta_seconds < 0 else ""
    s = abs(delta_seconds)
    if s < 60:
        return f"{sign}{s:.0f}s"
    if s < 3600:
        return f"{sign}{s / 60:.0f}m"
    h, rem = divmod(int(s), 3600)
    return f"{sign}{h}h{rem // 60:02d}m"


def job_state(job: dict[str, Any]) -> str:
    """Held, Released, Fallback or Running/Complete/Failed from the Job status."""
    spec = job.get("spec") or {}
    status = job.get("status") or {}
    ann = (job.get("metadata") or {}).get("annotations") or {}
    if spec.get("suspend"):
        return "Held" if ann.get(ANNOTATION_PLANNED_START) else "Pending"
    for cond in status.get("conditions") or []:
        if cond.get("status") == "True" and cond.get("type") in ("Complete", "Failed"):
            return str(cond["type"])
    if ann.get(ANNOTATION_FALLBACK_REASON):
        return "Fallback"
    if status.get("active"):
        return "Running"
    return "Released"


def job_row(job: dict[str, Any], now: datetime | None = None) -> dict[str, str]:
    """Flatten a Job (as a dict) into the columns ``status`` prints."""
    now = now or datetime.now(UTC)
    meta = job.get("metadata") or {}
    ann = meta.get("annotations") or {}
    planned = _parse_dt(ann.get(ANNOTATION_PLANNED_START))
    state = job_state(job)
    starts_in = _humanize((planned - now).total_seconds()) if planned and state == "Held" else ""
    share = ann.get(ANNOTATION_EXPECTED_RENEWABLE_SHARE)
    co2 = ann.get(ANNOTATION_CO2_AVOIDED)
    return {
        "namespace": str(meta.get("namespace", "")),
        "name": str(meta.get("name", "")),
        "state": state,
        "policy": str(ann.get(ANNOTATION_POLICY_USED, "")),
        "planned": planned.astimezone().strftime("%a %H:%M") if planned else "",
        "starts_in": starts_in,
        "share": f"{float(share):.0f}%" if share else "",
        "co2": f"{float(co2):.0f} g" if co2 else "",
        "reason": str(ann.get(ANNOTATION_FALLBACK_REASON, "")),
    }


def release_patch(now: datetime | None = None) -> dict[str, Any]:
    """Patch body that releases a held Job and records why."""
    now = now or datetime.now(UTC)
    return {
        "spec": {"suspend": False},
        "metadata": {
            "annotations": {
                ANNOTATION_ACTUAL_START: now.isoformat(),
                ANNOTATION_FALLBACK_REASON: "manual_release",
            }
        },
    }


def is_flexible(job: dict[str, Any]) -> bool:
    ann = (job.get("metadata") or {}).get("annotations") or {}
    return str(ann.get(ANNOTATION_FLEXIBLE, "")).lower() == "true"


# ------------------------------------------------------------ k8s access --


def _kube() -> tuple[Any, Any]:
    """The kubernetes ``client`` and ``config`` modules (typed loosely)."""
    from kubernetes import client, config

    return client, config


def _batch_api() -> Any:
    client, config = _kube()
    try:
        config.load_kube_config()
    except Exception:
        config.load_incluster_config()
    return client.BatchV1Api()


def _current_namespace() -> str:
    try:
        _, config = _kube()
        _, ctx = config.list_kube_config_contexts()
        return str(ctx.get("context", {}).get("namespace") or "default")
    except Exception:
        return "default"


def _list_jobs(namespace: str | None) -> list[dict[str, Any]]:
    api = _batch_api()
    raw = api.list_job_for_all_namespaces() if namespace is None else api.list_namespaced_job(namespace)
    jobs = [api.api_client.sanitize_for_serialization(j) for j in raw.items]
    return [j for j in jobs if is_flexible(j)]


# ------------------------------------------------------------------ CLI ---

STATE_STYLE = {
    "Held": "blue",
    "Running": "green",
    "Complete": "dim",
    "Released": "green",
    "Fallback": "yellow",
    "Failed": "red",
    "Pending": "magenta",
}


@click.group()
@click.version_option(prog_name="kubectl-carbon")
def main() -> None:
    """Inspect and steer carbon-aware batch Jobs."""


@main.command("status")
@click.option("-n", "--namespace", default=None, help="Namespace (default: current context)")
@click.option("-A", "--all-namespaces", is_flag=True, help="List across all namespaces")
def status_command(namespace: str | None, all_namespaces: bool) -> None:
    """List flexible Jobs with their plan."""
    ns = None if all_namespaces else (namespace or _current_namespace())
    rows = [job_row(j) for j in _list_jobs(ns)]
    print_status(rows, show_namespace=ns is None)


def print_status(rows: list[dict[str, str]], show_namespace: bool = True) -> None:
    """Render ``status`` rows as a table."""
    if not rows:
        console.print("[dim]No flexible Jobs found.[/dim]")
        return
    table = Table(header_style="bold", box=None, pad_edge=False)
    if show_namespace:
        table.add_column("NAMESPACE")
    for col in ("NAME", "STATE", "POLICY", "PLANNED", "STARTS IN", "SHARE", "CO₂ AVOIDED"):
        table.add_column(col, justify="right" if col in ("SHARE", "CO₂ AVOIDED") else "left")
    rows = sorted(rows, key=lambda r: (r["state"] != "Held", r["planned"]))
    for r in rows:
        style = STATE_STYLE.get(r["state"], "")
        cells = [
            r["name"],
            f"[{style}]{r['state']}[/{style}]" if style else r["state"],
            r["policy"],
            r["planned"],
            r["starts_in"],
            r["share"],
            r["co2"],
        ]
        table.add_row(*([r["namespace"]] if show_namespace else []), *cells)
    console.print(table)
    held = sum(1 for r in rows if r["state"] == "Held")
    total = sum(float(r["co2"].split()[0]) for r in rows if r["co2"])
    console.print(f"\n[bold]{held}[/bold] held · est. [green]{total:.0f} g CO₂[/green] avoided")


@main.command("explain")
@click.argument("name")
@click.option("-n", "--namespace", default=None, help="Namespace (default: current context)")
def explain_command(name: str, namespace: str | None) -> None:
    """Show every carbon-scheduler annotation on one Job."""
    ns = namespace or _current_namespace()
    api = _batch_api()
    job = api.api_client.sanitize_for_serialization(api.read_namespaced_job(name, ns))
    print_explain(job)


def print_explain(job: dict[str, Any]) -> None:
    """Pretty-print a Job's carbon decision."""
    meta = job.get("metadata") or {}
    ann = meta.get("annotations") or {}
    row = job_row(job)
    console.print(f"[bold]{meta.get('namespace')}/{meta.get('name')}[/bold]  state: {row['state']}")
    order = [
        ANNOTATION_DEADLINE,
        ANNOTATION_ESTIMATED_RUNTIME,
        ANNOTATION_POLICY_USED,
        ANNOTATION_PLANNED_START,
        ANNOTATION_LATEST_START,
        ANNOTATION_EXPECTED_RENEWABLE_SHARE,
        ANNOTATION_CO2_AVOIDED,
        ANNOTATION_FALLBACK_REASON,
        ANNOTATION_ACTUAL_START,
    ]
    keys = order + sorted(k for k in ann if k.startswith(ANNOTATION_PREFIX) and k not in order)
    table = Table(show_header=False, box=None)
    table.add_column(style="cyan")
    table.add_column()
    for key in keys:
        if key in ann:
            table.add_row(key.removeprefix(ANNOTATION_PREFIX + "/"), str(ann[key]))
    console.print(table)
    if row["starts_in"]:
        console.print(f"\nReleases in [bold]{row['starts_in']}[/bold] at {row['planned']}.")


@main.command("release")
@click.argument("name")
@click.option("-n", "--namespace", default=None, help="Namespace (default: current context)")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation")
def release_command(name: str, namespace: str | None, yes: bool) -> None:
    """Release a held Job now, skipping its green window."""
    ns = namespace or _current_namespace()
    if not yes:
        click.confirm(f"Release {ns}/{name} now?", abort=True)
    _batch_api().patch_namespaced_job(name, ns, release_patch())
    console.print(f"[green]Released {ns}/{name}[/green] (fallback-reason: manual_release)")


@main.command("forecast")
@click.option("--country", default="de", show_default=True)
@click.option("--hours", default=24, show_default=True, help="Hours ahead to show")
def forecast_command(country: str, hours: int) -> None:
    """Show the renewable-share forecast and the greenest upcoming hour."""
    from carbon_scheduler.sim.cli import _fetch_live_forecast
    from carbon_scheduler.sim.plan import sparkline

    forecast = asyncio.run(_fetch_live_forecast(country))
    now = datetime.now(UTC)
    pts = [p for p in forecast.points if 0 <= (p.timestamp - now).total_seconds() <= hours * 3600]
    if not pts:
        raise click.ClickException("No forecast data available")
    shares = [p.renewable_share_pct for p in pts]
    best = max(pts, key=lambda p: p.renewable_share_pct)
    console.print(f"[bold]Renewable share, next {hours}h ({country.upper()})[/bold]")
    console.print(f"  [green]{sparkline(shares)}[/green]")
    console.print(
        f"  now {shares[0]:.0f}%   min {min(shares):.0f}%   max {max(shares):.0f}% "
        f"at {best.timestamp.astimezone():%a %H:%M}"
    )


if __name__ == "__main__":
    main()
