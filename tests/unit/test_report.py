"""Tests for carbon-sim synth, report and showcase."""

from pathlib import Path

from click.testing import CliRunner

from carbon_scheduler.sim.backtest import BacktestResult
from carbon_scheduler.sim.cli import main
from carbon_scheduler.sim.historical import HistoricalDataStore
from carbon_scheduler.sim.report import best_policy, render_report


def test_showcase_end_to_end(tmp_path: Path) -> None:
    out = tmp_path / "sc"
    result = CliRunner().invoke(
        main, ["showcase", "-o", str(out), "--days", "7", "--jobs", "40", "--seed", "1"]
    )
    assert result.exit_code == 0, result.output
    for name in ("results.csv", "trace.json", "report.html"):
        assert (out / name).exists()
    html = (out / "report.html").read_text()
    assert "<svg" in html and "balanced" in html and "greenest-window" in html
    assert "http" not in html.split("<footer>")[0].replace("http-equiv", "")  # no external assets

    res = BacktestResult.load_csv(out / "results.csv")
    assert set(res.policy_results) == {"immediate", "threshold", "greenest-window", "balanced"}
    assert all(pr.deadline_misses == 0 for pr in res.policy_results.values())
    best = best_policy(res)
    assert best is not None and res.get_avoided_co2(best) > 0


def test_synth_and_report_commands(tmp_path: Path) -> None:
    runner = CliRunner()
    data = tmp_path / "data"
    r = runner.invoke(main, ["synth", "--start", "2026-03-01", "--end", "2026-03-03", "-o", str(data)])
    assert r.exit_code == 0, r.output
    hist = HistoricalDataStore(data).load()
    assert hist is not None and len(hist.points) == 3 * 96

    sc = tmp_path / "sc"
    runner.invoke(main, ["showcase", "-o", str(sc), "--days", "3", "--jobs", "10"])
    report = tmp_path / "r.html"
    r = runner.invoke(
        main,
        ["report", str(sc / "results.csv"), "--data", str(data), "-o", str(report), "--title", "T<x>"],
    )
    assert r.exit_code == 0, r.output
    assert "T&lt;x&gt;" in report.read_text()


def test_synth_rejects_reversed_dates(tmp_path: Path) -> None:
    r = CliRunner().invoke(
        main, ["synth", "--start", "2026-03-05", "--end", "2026-03-01", "-o", str(tmp_path)]
    )
    assert r.exit_code != 0


def test_render_report_without_baseline() -> None:
    html = render_report(BacktestResult())
    assert "Best policy" in html
    assert best_policy(BacktestResult()) is None
