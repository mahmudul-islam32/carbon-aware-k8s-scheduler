"""Render real CLI output as SVG "screenshots" for the README.

    python docs/media/terminal.py      (or: make media)

Runs ``carbon-sim plan``, ``carbon-sim showcase`` and ``kubectl carbon status``
against synthetic data with a recording rich Console and exports SVGs.
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from click.testing import CliRunner
from rich.console import Console
from rich.terminal_theme import MONOKAI

from carbon_scheduler import kubectl_plugin
from carbon_scheduler.config import (
    ANNOTATION_CO2_AVOIDED,
    ANNOTATION_EXPECTED_RENEWABLE_SHARE,
    ANNOTATION_FALLBACK_REASON,
    ANNOTATION_FLEXIBLE,
    ANNOTATION_PLANNED_START,
    ANNOTATION_POLICY_USED,
)
from carbon_scheduler.sim import cli
from carbon_scheduler.sim.synthetic import synthetic_shares

OUT = Path(__file__).parent
NOW = datetime(2026, 6, 15, 5, 0, tzinfo=UTC)


def _recorder(width: int = 104) -> Console:
    return Console(record=True, width=width, force_terminal=True, color_system="truecolor")


def _save(console: Console, name: str, title: str) -> None:
    console.save_svg(str(OUT / name), title=title, theme=MONOKAI)
    print("wrote", OUT / name)


def plan_svg() -> None:
    pts = synthetic_shares(NOW, hours=36, step_minutes=15, seed=3)
    with tempfile.TemporaryDirectory() as tmp:
        fc = Path(tmp) / "forecast.json"
        fc.write_text(
            json.dumps(
                {
                    "unix_seconds": [int(ts.timestamp()) for ts, _ in pts],
                    "ren_share": [s for _, s in pts],
                    "substitute": False,
                }
            )
        )
        cli.console = rec = _recorder(84)
        rec.print("[bold]$[/bold] carbon-sim plan --runtime 2h --within 14h --cpu 32\n")
        CliRunner().invoke(
            cli.main,
            ["plan", "--runtime", "2h", "--within", "14h", "--cpu", "32",
             "--forecast-file", str(fc), "--now", NOW.isoformat()],
            catch_exceptions=False,
        )
        _save(rec, "terminal-plan.svg", "carbon-sim plan")


def showcase_svg() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cli.console = rec = _recorder(118)
        rec.print("[bold]$[/bold] carbon-sim showcase --days 30 --jobs 300\n")
        CliRunner().invoke(
            cli.main, ["showcase", "--output", tmp, "--days", "30", "--jobs", "300"],
            catch_exceptions=False,
        )
        _save(rec, "terminal-showcase.svg", "carbon-sim showcase")


def kubectl_svg() -> None:
    def job(ns: str, name: str, suspend: bool, hours: float, share: float, co2: float,
            policy: str = "greenest-window", **extra: str) -> dict:
        return {
            "metadata": {
                "namespace": ns,
                "name": name,
                "annotations": {
                    ANNOTATION_FLEXIBLE: "true",
                    ANNOTATION_PLANNED_START: (datetime.now(UTC) + timedelta(hours=hours)).isoformat(),
                    ANNOTATION_EXPECTED_RENEWABLE_SHARE: str(share),
                    ANNOTATION_CO2_AVOIDED: str(co2),
                    ANNOTATION_POLICY_USED: policy,
                    **extra,
                },
            },
            "spec": {"suspend": suspend},
            "status": {"active": 1} if hours > -1 and not suspend else {},
        }

    jobs = [
        job("data", "nightly-etl", True, 3.4, 81.2, 214),
        job("ml", "resnet-train", True, 5.1, 84.0, 1630),
        job("ml", "feature-refresh", True, 1.25, 72.5, 96, "balanced"),
        job("media", "video-transcode", False, -0.5, 77.1, 341, "balanced"),
        job("finance", "report-gen", False, -2.0, 64.3, 12, "threshold"),
        job("finance", "invoice-batch", False, -3.0, 41.0, 0, "immediate",
            **{ANNOTATION_FALLBACK_REASON: "current_time_past_latest_feasible_start"}),
    ]
    kubectl_plugin.console = rec = _recorder(100)
    rec.print("[bold]$[/bold] kubectl carbon status -A\n")
    kubectl_plugin.print_status([kubectl_plugin.job_row(j) for j in jobs], show_namespace=True)
    _save(rec, "terminal-kubectl.svg", "kubectl carbon status")


if __name__ == "__main__":
    plan_svg()
    showcase_svg()
    kubectl_svg()
