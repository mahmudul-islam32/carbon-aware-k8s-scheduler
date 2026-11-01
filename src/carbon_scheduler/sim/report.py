"""Self-contained HTML report for backtest results.

``carbon-sim report results.csv --data data/ -o report.html`` renders one HTML
file with inline SVG charts and no external assets, so it can be attached to a
pull request, e-mailed or published as a CI artifact.
"""

from __future__ import annotations

import html
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from carbon_scheduler.sim.backtest import BacktestResult, PolicyResult
from carbon_scheduler.sim.historical import HistoricalData

BAR = "var(--series-1)"
POLICY_ORDER = ("immediate", "threshold", "greenest-window", "balanced")


def _esc(value: object) -> str:
    return html.escape(str(value))


def _fmt_hours(seconds: float) -> str:
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def _ordered(result: BacktestResult) -> list[tuple[str, PolicyResult]]:
    known = [(n, result.policy_results[n]) for n in POLICY_ORDER if n in result.policy_results]
    rest = [(n, r) for n, r in result.policy_results.items() if n not in POLICY_ORDER]
    return known + rest


def best_policy(result: BacktestResult) -> str | None:
    """The policy with the largest CO2 saving and no deadline misses."""
    best, best_saving = None, 0.0
    for name, pr in result.policy_results.items():
        if name == result.baseline_policy or pr.deadline_misses:
            continue
        saving = result.get_avoided_co2(name)
        if saving > best_saving:
            best, best_saving = name, saving
    return best


# ---------------------------------------------------------------- charts --


def _hbar_chart(
    rows: list[tuple[str, float, str]],
    unit: str,
    highlight: str | None = None,
    width: int = 520,
) -> str:
    """Horizontal bars: (label, value, value_text)."""
    row_h, label_w, value_w = 34, 130, 90
    height = row_h * len(rows) + 8
    vmax = max((v for _, v, _ in rows), default=1.0) or 1.0
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{_esc(unit)}">']
    for i, (label, value, text) in enumerate(rows):
        y = 4 + i * row_h
        bw = max(3.0, (width - label_w - value_w) * value / vmax)
        fill = "var(--accent)" if label == highlight else BAR
        weight = "600" if label == highlight else "400"
        parts.append(
            f'<g><title>{_esc(label)}: {_esc(text)}</title>'
            f'<text x="0" y="{y + 20}" class="lbl" style="font-weight:{weight}">{_esc(label)}</text>'
            f'<rect x="{label_w}" y="{y + 8}" width="{bw:.1f}" height="16" rx="4" fill="{fill}"/>'
            f'<text x="{label_w + bw + 8:.1f}" y="{y + 21}" class="val">{_esc(text)}</text></g>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _tradeoff_chart(result: BacktestResult, width: int = 520, height: int = 260) -> str:
    """Mean delay (x) vs CO2 saving % (y), one labelled dot per policy."""
    pts = [
        (name, pr.mean_delay_seconds / 3600, result.get_avoided_co2_pct(name))
        for name, pr in _ordered(result)
    ]
    m = {"l": 48, "r": 20, "t": 16, "b": 36}
    xmax = max([p[1] for p in pts] + [1.0]) * 1.2
    ylo = min([p[2] for p in pts] + [0.0])
    yhi = max([p[2] for p in pts] + [1.0]) * 1.2

    def x(v: float) -> float:
        return m["l"] + v / xmax * (width - m["l"] - m["r"])

    def y(v: float) -> float:
        return m["t"] + (1 - (v - ylo) / ((yhi - ylo) or 1)) * (height - m["t"] - m["b"])

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Delay vs savings">']
    for i in range(5):
        v = ylo + (yhi - ylo) * i / 4
        parts.append(
            f'<line x1="{m["l"]}" x2="{width - m["r"]}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>'
            f'<text x="{m["l"] - 8}" y="{y(v) + 4:.1f}" class="tick" text-anchor="end">{v:.0f}%</text>'
        )
        hv = xmax * i / 4
        parts.append(
            f'<text x="{x(hv):.1f}" y="{height - 16}" class="tick" text-anchor="middle">{hv:.1f}h</text>'
        )
    parts.append(
        f'<text x="{(width + m["l"]) / 2}" y="{height - 2}" class="tick" text-anchor="middle">'
        "mean delay →</text>"
    )
    for name, dx, dy in pts:
        parts.append(
            f'<g><title>{_esc(name)}: {dy:.1f}% CO₂ saved, mean delay {dx:.1f} h</title>'
            f'<circle cx="{x(dx):.1f}" cy="{y(dy):.1f}" r="6" fill="{BAR}" stroke="var(--surface)" stroke-width="2"/>'
            + (
                f'<text x="{x(dx) - 10:.1f}" y="{y(dy) + 4:.1f}" class="lbl" text-anchor="end">'
                if x(dx) > width * 0.7
                else f'<text x="{x(dx) + 10:.1f}" y="{y(dy) + 4:.1f}" class="lbl">'
            )
            + f"{_esc(name)}</text></g>"
        )
    parts.append("</svg>")
    return "".join(parts)


def _histogram(values: Iterable[float], bins: list[float], width: int = 250, height: int = 110) -> str:
    """Small delay histogram (values in hours)."""
    counts = [0] * (len(bins) - 1)
    for v in values:
        for i in range(len(bins) - 1):
            if bins[i] <= v < bins[i + 1] or (i == len(bins) - 2 and v >= bins[-1]):
                counts[i] += 1
                break
    cmax = max(counts) or 1
    m_b = 18
    bw = width / len(counts)
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="delay histogram">']
    parts.append(f'<line x1="0" x2="{width}" y1="{height - m_b}" y2="{height - m_b}" class="grid"/>')
    for i, c in enumerate(counts):
        h = (height - m_b - 6) * c / cmax
        label = f"{bins[i]:g}–{bins[i + 1]:g} h" if i < len(counts) - 1 else f"≥ {bins[i]:g} h"
        parts.append(
            f'<g><title>{label}: {c} jobs</title>'
            f'<rect x="{i * bw + 1:.1f}" y="{height - m_b - h:.1f}" width="{bw - 2:.1f}" '
            f'height="{max(h, 0.5):.1f}" rx="2" fill="{BAR}"/></g>'
        )
        if i % 2 == 0:
            parts.append(
                f'<text x="{i * bw + bw / 2:.1f}" y="{height - 4}" class="tick" '
                f'text-anchor="middle">{bins[i]:g}</text>'
            )
    parts.append("</svg>")
    return "".join(parts)


def _grid_chart(data: HistoricalData, width: int = 1080, height: int = 200) -> str:
    """Renewable share across the backtest period (downsampled to ~hourly)."""
    pts = data.points
    if len(pts) < 2:
        return ""
    step = max(1, len(pts) // 1200)
    sample = pts[::step]
    t0 = sample[0].timestamp.timestamp()
    t1 = sample[-1].timestamp.timestamp()
    m = {"l": 40, "r": 8, "t": 8, "b": 22}

    def x(ts: float) -> float:
        return m["l"] + (ts - t0) / ((t1 - t0) or 1) * (width - m["l"] - m["r"])

    def y(v: float) -> float:
        return m["t"] + (1 - v / 100) * (height - m["t"] - m["b"])

    line = "".join(
        f"{'M' if i == 0 else 'L'}{x(p.timestamp.timestamp()):.1f},{y(p.renewable_share_pct):.1f}"
        for i, p in enumerate(sample)
    )
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="renewable share">']
    for v in (0, 50, 100):
        parts.append(
            f'<line x1="{m["l"]}" x2="{width - m["r"]}" y1="{y(v)}" y2="{y(v)}" class="grid"/>'
            f'<text x="{m["l"] - 6}" y="{y(v) + 4}" class="tick" text-anchor="end">{v}%</text>'
        )
    start = sample[0].timestamp
    days = max(1, int((t1 - t0) // 86400))
    every = max(1, days // 8)
    for d in range(0, days + 1, every):
        ts = (start + timedelta(days=d)).timestamp()
        parts.append(
            f'<text x="{x(ts):.1f}" y="{height - 4}" class="tick" text-anchor="middle">'
            f"{datetime.fromtimestamp(ts, tz=UTC):%b %d}</text>"
        )
    parts.append(
        f'<path d="{line}L{x(t1):.1f},{y(0)}L{x(t0):.1f},{y(0)}Z" fill="var(--share-fill)"/>'
        f'<path d="{line}" fill="none" stroke="var(--share)" stroke-width="1.5"/>'
    )
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------- page ----

CSS = """
:root{color-scheme:light;--bg:#f5f6f3;--surface:#fcfcfb;--border:#e3e4df;--grid:#e9eae5;--text:#0b0b0b;
--text-2:#52514e;--text-3:#7a7974;--series-1:#2a78d6;--accent:#15803d;--share:#1baf7a;--share-fill:rgba(27,175,122,.16)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#111110;--surface:#1a1a19;
--border:#2e2e2b;--grid:#2a2a27;--text:#f0efec;--text-2:#c3c2b7;--text-3:#8f8e86;--series-1:#3987e5;--accent:#22a355;
--share:#199e70;--share-fill:rgba(25,158,112,.22)}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#111110;--surface:#1a1a19;--border:#2e2e2b;--grid:#2a2a27;--text:#f0efec;
--text-2:#c3c2b7;--text-3:#8f8e86;--series-1:#3987e5;--accent:#22a355;--share:#199e70;--share-fill:rgba(25,158,112,.22)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif}
.wrap{max-width:1140px;margin:0 auto;padding:28px 16px 48px}
h1{font-size:24px;letter-spacing:-.02em;margin:0 0 4px}h2{font-size:15px;margin:0 0 4px}
.sub{color:var(--text-2);margin:0 0 20px}.hint{color:var(--text-3);font-size:12px;margin:0 0 10px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px 18px;margin-bottom:14px;min-width:0}
.kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin-bottom:14px}
.kpi .l{color:var(--text-2);font-size:12px}.kpi .v{font-size:26px;font-weight:650;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.kpi .s{color:var(--text-3);font-size:12px}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.grid4{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}
svg{display:block;width:100%;height:auto}svg text{font-family:inherit}
.lbl{fill:var(--text-2);font-size:12px}.val{fill:var(--text);font-size:12px}.tick{fill:var(--text-3);font-size:11px}
.grid{stroke:var(--grid);stroke-width:1}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th{color:var(--text-3);font-weight:500;text-align:right;padding:6px 8px;border-bottom:1px solid var(--border);white-space:nowrap}
td{text-align:right;padding:7px 8px;border-bottom:1px solid var(--grid);white-space:nowrap}
th:first-child,td:first-child{text-align:left}.best{color:var(--accent);font-weight:600}
.tbl{overflow-x:auto}footer{color:var(--text-3);font-size:12px;margin-top:18px}
@media(max-width:860px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.grid2,.grid4{grid-template-columns:1fr}}
"""


def render_report(
    result: BacktestResult,
    historical: HistoricalData | None = None,
    title: str = "Carbon-aware scheduling backtest",
) -> str:
    """Render a backtest result as a standalone HTML page."""
    ordered = _ordered(result)
    best = best_policy(result)
    baseline = result.policy_results.get(result.baseline_policy)
    n_jobs = max((pr.total_jobs for _, pr in ordered), default=0)

    best_pr = result.policy_results.get(best) if best else None
    saved_kg = result.get_avoided_co2(best) / 1000 if best else 0.0
    saved_pct = result.get_avoided_co2_pct(best) if best else 0.0
    share_gain = (
        best_pr.mean_renewable_share - baseline.mean_renewable_share
        if best_pr is not None and baseline is not None
        else 0.0
    )
    period = ""
    if historical is not None and historical.start_time and historical.end_time:
        period = f"{historical.start_time:%d %b %Y} – {historical.end_time:%d %b %Y}"

    kpis = [
        ("Best policy", _esc(best or "—"), "no deadline misses" if best else "no policy beat baseline"),
        ("CO₂ saved", f"{saved_pct:.1f}%", f"{saved_kg:.2f} kg vs. immediate start"),
        ("Renewable share", f"+{share_gain:.1f} pts", "mean share during job runs"),
        ("Mean delay", _fmt_hours(best_pr.mean_delay_seconds) if best_pr else "—", "submit → start"),
        ("Jobs simulated", f"{n_jobs}", period or "per policy"),
    ]
    kpi_html = "".join(
        f'<div class="card kpi"><div class="l">{label}</div>'
        f'<div class="v"{" style=\'font-size:20px\'" if len(value) > 10 else ""}>{value}</div>'
        f'<div class="s">{_esc(sub)}</div></div>'
        for label, value, sub in kpis
    )

    co2_rows = [(n, pr.total_co2_grams / 1000, f"{pr.total_co2_grams / 1000:.2f} kg") for n, pr in ordered]
    share_rows = [(n, pr.mean_renewable_share, f"{pr.mean_renewable_share:.1f}%") for n, pr in ordered]

    bins: list[float] = [0, 1, 2, 3, 4, 6, 8, 12]
    hist_html = "".join(
        f'<div><h2 style="font-size:13px">{_esc(n)}</h2>'
        f'<p class="hint">mean {_fmt_hours(pr.mean_delay_seconds)}</p>'
        f'{_histogram((j.delay.total_seconds() / 3600 for j in pr.job_results), bins)}</div>'
        for n, pr in ordered
    )

    table_rows = []
    for n, pr in ordered:
        cls = ' class="best"' if n == best else ""
        avoided = "—" if n == result.baseline_policy else f"{result.get_avoided_co2(n) / 1000:.2f}"
        pct = "—" if n == result.baseline_policy else f"{result.get_avoided_co2_pct(n):.1f}%"
        table_rows.append(
            f"<tr><td{cls}>{_esc(n)}{' ★' if n == best else ''}</td><td>{pr.total_jobs}</td>"
            f"<td>{pr.deadline_misses}</td><td>{_fmt_hours(pr.mean_delay_seconds)}</td>"
            f"<td>{pr.mean_renewable_share:.1f}%</td><td>{pr.total_energy_kwh:.1f}</td>"
            f"<td>{pr.total_co2_grams / 1000:.2f}</td><td>{avoided}</td><td>{pct}</td>"
            f"<td>{pr.fallback_count}</td></tr>"
        )

    grid_html = ""
    if historical is not None:
        chart = _grid_chart(historical)
        if chart:
            grid_html = (
                '<section class="card"><h2>Grid renewable share during the backtest</h2>'
                f'<p class="hint">{_esc(period)} · used as a perfect forecast</p>{chart}</section>'
            )

    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backtest Report</title><style>{CSS}</style></head>
<body><div class="wrap">
<h1>🌱 {_esc(title)}</h1>
<p class="sub">How much CO₂ each scheduling policy would have avoided on the same job trace, compared with starting every job at submission.</p>
<section class="kpis">{kpi_html}</section>
{grid_html}
<section class="grid2">
  <div class="card"><h2>Total CO₂ emitted</h2><p class="hint">kg, lower is better · best policy in green</p>{_hbar_chart(co2_rows, "kg CO2", best)}</div>
  <div class="card"><h2>Mean renewable share while running</h2><p class="hint">% of generation, higher is better</p>{_hbar_chart(share_rows, "renewable share", best)}</div>
</section>
<section class="grid2">
  <div class="card"><h2>Savings vs. waiting time</h2><p class="hint">every policy keeps deadlines; how long do jobs wait for their saving?</p>{_tradeoff_chart(result)}</div>
  <div class="card"><h2>Policy comparison</h2><p class="hint">energy in kWh, CO₂ in kg</p><div class="tbl"><table>
  <thead><tr><th>Policy</th><th>Jobs</th><th>Misses</th><th>Mean delay</th><th>Share</th><th>Energy</th><th>CO₂</th><th>Avoided</th><th>Saving</th><th>Fallbacks</th></tr></thead>
  <tbody>{"".join(table_rows)}</tbody></table></div></div>
</section>
<section class="card"><h2>Delay distribution</h2><p class="hint">jobs per delay bucket (hours)</p><div class="grid4">{hist_html}</div></section>
<footer>Generated {generated} by carbon-sim · CO₂ figures are estimates derived from the renewable share and a fossil intensity of 400 gCO₂/kWh. Grid data: Energy-Charts.info (Fraunhofer ISE), CC BY 4.0, or synthetic.</footer>
</div></body></html>
"""
