"""Generate the SVG infographics used in README.md and docs/.

Edit the data below (feature statuses in LANES, roadmap waves in WAVES), then run:

    python docs/assets/generate.py

Every SVG in docs/assets is rewritten. Pure standard library, no dependencies.
"""

import html
import math
from pathlib import Path

OUT = Path(__file__).resolve().parent
FONT = "font-family=\"'Segoe UI', Inter, Helvetica, Arial, sans-serif\""
INK, MUTED, LINE, CARD, PAGE = "#0f172a", "#475569", "#e2e8f0", "#ffffff", "#f8fafc"
STATUS = {
    "done":     ("#16a34a", "#dcfce7", "Done"),
    "new":      ("#2563eb", "#dbeafe", "New"),
    "improved": ("#0d9488", "#ccfbf1", "Improved"),
    "planned":  ("#d97706", "#fef3c7", "Planned"),
}
def esc(t): return html.escape(t, quote=True)

def frame(w, h, title, subtitle, body, label):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-label="{esc(label)}">
  <rect width="{w}" height="{h}" rx="20" fill="{PAGE}" stroke="{LINE}"/>
  <g {FONT}>
    <text x="40" y="54" font-size="24" font-weight="800" fill="{INK}">{esc(title)}</text>
    <text x="40" y="80" font-size="14" fill="{MUTED}">{esc(subtitle)}</text>
{body}
  </g>
</svg>
'''

# ---------------------------------------------------------------- how it works
def how_it_works():
    steps = [
        ("1", "Annotate", "#16a34a", ["Submit a Job with", "suspend: true plus", "flexible, deadline and", "estimated-runtime"], "tag"),
        ("2", "Forecast", "#0d9488", ["Fetch the renewable-share", "forecast from Energy-Charts", "(Fraunhofer ISE),", "cached for 30 min"], "sun"),
        ("3", "Plan", "#2563eb", ["A policy picks the greenest", "window before the latest", "start and annotates the", "plan and est. CO₂ saved"], "clock"),
        ("4", "Release & measure", "#7c3aed", ["The timer sets", "suspend: false, posts an", "Event and exports metrics", "to Prometheus/Grafana"], "play"),
    ]
    w, h = 1200, 360
    cw, gap, x0, y0 = 258, 36, 40, 110
    out = []
    for i, (num, title, color, lines, icon) in enumerate(steps):
        x = x0 + i * (cw + gap)
        out.append(f'    <rect x="{x}" y="{y0}" width="{cw}" height="208" rx="16" fill="{CARD}" stroke="{LINE}"/>')
        out.append(f'    <rect x="{x}" y="{y0}" width="{cw}" height="6" rx="3" fill="{color}"/>')
        cx, cy = x + 44, y0 + 50
        out.append(f'    <circle cx="{cx}" cy="{cy}" r="24" fill="{color}" fill-opacity="0.12"/>')
        if icon == "tag":
            out.append(f'    <path d="M{cx-11} {cy-11} h12 l11 11 l-12 12 l-11 -11 z" fill="none" stroke="{color}" stroke-width="2.5" stroke-linejoin="round"/><circle cx="{cx-5}" cy="{cy-5}" r="2.5" fill="{color}"/>')
        elif icon == "sun":
            rays = "".join(f'<line x1="{cx+11*math.cos(a):.1f}" y1="{cy+11*math.sin(a):.1f}" x2="{cx+16*math.cos(a):.1f}" y2="{cy+16*math.sin(a):.1f}" stroke="{color}" stroke-width="2.5" stroke-linecap="round"/>' for a in [k*math.pi/4 for k in range(8)])
            out.append(f'    <circle cx="{cx}" cy="{cy}" r="7" fill="{color}"/>{rays}')
        elif icon == "clock":
            out.append(f'    <circle cx="{cx}" cy="{cy}" r="13" fill="none" stroke="{color}" stroke-width="2.5"/><path d="M{cx} {cy-8} V{cy} L{cx+6} {cy+4}" fill="none" stroke="{color}" stroke-width="2.5" stroke-linecap="round"/>')
        else:
            out.append(f'    <path d="M{cx-6} {cy-10} L{cx+10} {cy} L{cx-6} {cy+10} z" fill="{color}"/>')
        out.append(f'    <text x="{x+cw-22}" y="{y0+62}" font-size="40" font-weight="800" fill="{color}" fill-opacity="0.18" text-anchor="end">{num}</text>')
        out.append(f'    <text x="{x+22}" y="{y0+102}" font-size="18" font-weight="800" fill="{INK}">{esc(title)}</text>')
        for j, line in enumerate(lines):
            out.append(f'    <text x="{x+22}" y="{y0+128+j*18}" font-size="13" fill="{MUTED}">{esc(line)}</text>')
        if i < len(steps) - 1:
            ax = x + cw + 6
            out.append(f'    <path d="M{ax} {y0+95} h{gap-14}" stroke="#94a3b8" stroke-width="2.5"/><path d="M{ax+gap-14} {y0+89} l8 6 l-8 6 z" fill="#94a3b8"/>')
    out.append(f'    <text x="40" y="{h-18}" font-size="12" fill="{MUTED}">Fail-open: if annotations are invalid or grid data is missing or degraded, the Job is released immediately. Deadlines always win.</text>')
    return frame(w, h, "How it works", "Four steps from kubectl apply to a greener start time", "\n".join(out),
                 "How it works: annotate, forecast, plan, release and measure")

# ---------------------------------------------------------------- feature map
LANES = [
    ("Core control loop", "#16a34a", [
        ("F-01", "Annotation contract", "done"), ("F-02", "Suspend & release", "improved"),
        ("F-05", "Deadline guarantee", "done"), ("F-12", "Kubernetes Events", "improved"),
        ("F-14", "Per-job overrides", "new"), ("F-15", "Max-delay cap", "new"),
        ("F-21", "Mutating webhook", "planned"), ("F-22", "Relative deadlines", "new"),
        ("F-27", "HA leader election", "planned")]),
    ("Grid data", "#0d9488", [
        ("F-03", "Energy-Charts client", "done"), ("F-20", "Re-plan on refresh", "planned"),
        ("F-28", "More data providers", "planned")]),
    ("Planning", "#2563eb", [
        ("F-04", "Window policies", "done"), ("F-23", "Lowest-carbon policy", "planned"),
        ("F-26", "Release staggering", "planned"), ("F-30", "Runtime learning", "planned"),
        ("F-24", "Spatial shifting", "planned")]),
    ("Carbon accounting", "#65a30d", [
        ("F-06", "CO₂ estimation", "done"), ("F-16", "Live CO₂ per job", "new"),
        ("F-25", "Team carbon budgets", "planned"), ("F-31", "Measured energy", "planned")]),
    ("Observability", "#7c3aed", [
        ("F-07", "Prometheus metrics", "done"), ("F-08", "Grafana dashboard", "done"),
        ("F-13", "Health probes", "new"), ("F-19", "Grid & API metrics", "new"),
        ("F-32", "Alert rules", "planned")]),
    ("Tooling & delivery", "#db2777", [
        ("F-09", "Backtest CLI", "done"), ("F-10", "Helm chart & RBAC", "done"),
        ("F-17", "Dry-run planner", "new"), ("F-18", "CI pipeline", "new"),
        ("F-33", "E2E tests on kind", "planned"), ("F-34", "Release pipeline", "planned"),
        ("F-29", "kubectl plugin", "planned"), ("F-11", "KEDA mode", "planned")]),
]
def feature_map():
    lw, gap, x0, y0, th, tg = 178, 12, 40, 150, 40, 8
    rows = max(len(t) for _, _, t in LANES)
    h = y0 + 52 + rows * (th + tg) + 40
    w = x0 * 2 + len(LANES) * lw + (len(LANES) - 1) * gap
    out = []
    # legend + counts
    counts = {k: sum(1 for _, _, t in LANES for f in t if f[2] == k) for k in STATUS}
    lx = 40
    for k in ("done", "improved", "new", "planned"):
        c, bg, label = STATUS[k]
        out.append(f'    <rect x="{lx}" y="100" width="128" height="30" rx="15" fill="{bg}" stroke="{c}" stroke-opacity="0.5"/>')
        out.append(f'    <circle cx="{lx+18}" cy="115" r="5" fill="{c}"/>')
        out.append(f'    <text x="{lx+30}" y="120" font-size="13" font-weight="700" fill="{c}">{label}</text>')
        out.append(f'    <text x="{lx+114}" y="120" font-size="13" font-weight="800" fill="{c}" text-anchor="end">{counts[k]}</text>')
        lx += 140
    total = sum(counts.values())
    shipped = counts["done"] + counts["improved"] + counts["new"]
    bx, bw = 620, w - 620 - 40
    out.append(f'    <text x="{bx}" y="108" font-size="12" font-weight="700" fill="{MUTED}">SHIPPED {shipped} / {total}</text>')
    acc = bx
    for k in ("done", "improved", "new", "planned"):
        seg = bw * counts[k] / total
        out.append(f'    <rect x="{acc:.1f}" y="116" width="{seg:.1f}" height="12" fill="{STATUS[k][0]}" fill-opacity="{1 if k != "planned" else 0.35}"/>')
        acc += seg
    for i, (lane, color, tiles) in enumerate(LANES):
        x = x0 + i * (lw + gap)
        out.append(f'    <rect x="{x}" y="{y0}" width="{lw}" height="{h - y0 - 30}" rx="14" fill="{CARD}" stroke="{LINE}"/>')
        out.append(f'    <rect x="{x}" y="{y0}" width="{lw}" height="40" rx="14" fill="{color}"/>')
        out.append(f'    <rect x="{x}" y="{y0+26}" width="{lw}" height="14" fill="{color}"/>')
        out.append(f'    <text x="{x+lw/2}" y="{y0+26}" font-size="14" font-weight="800" fill="#ffffff" text-anchor="middle">{esc(lane)}</text>')
        for j, (fid, name, st) in enumerate(tiles):
            c, bg, _ = STATUS[st]
            ty = y0 + 52 + j * (th + tg)
            dash = ' stroke-dasharray="4 3"' if st == "planned" else ""
            out.append(f'    <rect x="{x+8}" y="{ty}" width="{lw-16}" height="{th}" rx="9" fill="{bg}" stroke="{c}" stroke-opacity="0.55"{dash}/>')
            out.append(f'    <rect x="{x+8}" y="{ty}" width="5" height="{th}" rx="2.5" fill="{c}"/>')
            out.append(f'    <text x="{x+20}" y="{ty+16}" font-size="11" font-weight="800" fill="{c}">{fid}</text>')
            out.append(f'    <text x="{x+20}" y="{ty+32}" font-size="12.5" font-weight="600" fill="{INK}">{esc(name)}</text>')
    return frame(w, h, "Feature status board", "Every feature by area. Dashed tiles are the backlog, ready to pick up.", "\n".join(out),
                 "Feature status board grouped by area")

# ---------------------------------------------------------------- roadmap
WAVES = [
    ("NOW", "Small, high impact · next 1–2 weeks", "#16a34a", [
        ("F-32", "PrometheusRule alert pack", "S"), ("F-34", "Image + chart release pipeline", "S"),
        ("F-20", "Re-plan on forecast refresh", "M")]),
    ("NEXT", "Hardening & scale · next month", "#2563eb", [
        ("F-21", "Mutating webhook", "M"), ("F-26", "Release staggering", "M"),
        ("F-27", "HA with leader election", "M"), ("F-33", "E2E tests on kind in CI", "M"),
        ("F-23", "Lowest-carbon policy", "M")]),
    ("LATER", "Exploration · research track", "#7c3aed", [
        ("F-11", "KEDA ScaledJob mode", "L"), ("F-24", "Spatial (multi-region) shifting", "L"),
        ("F-25", "Team carbon budgets", "L"), ("F-28", "More grid data providers", "L"),
        ("F-29", "kubectl carbon plugin", "M"), ("F-30", "Runtime learning", "M"),
        ("F-31", "Measured energy via Kepler", "L")]),
]
EFFORT = {"S": "#16a34a", "M": "#d97706", "L": "#dc2626"}
def roadmap():
    cw, gap, x0, y0, ih = 360, 20, 40, 110, 46
    rows = max(len(i) for *_, i in WAVES)
    h = y0 + 86 + rows * (ih + 8) + 60
    w = x0 * 2 + 3 * cw + 2 * gap
    out = []
    out.append(f'    <path d="M{x0} {y0+30} H{w-x0-12}" stroke="#cbd5e1" stroke-width="4" stroke-linecap="round"/>')
    out.append(f'    <path d="M{w-x0-14} {y0+22} l14 8 l-14 8 z" fill="#cbd5e1"/>')
    for i, (wave, sub, color, items) in enumerate(WAVES):
        x = x0 + i * (cw + gap)
        out.append(f'    <rect x="{x+44}" y="{y0+6}" width="250" height="50" fill="{PAGE}"/>')
        out.append(f'    <circle cx="{x+28}" cy="{y0+30}" r="14" fill="{color}"/><circle cx="{x+28}" cy="{y0+30}" r="6" fill="#ffffff"/>')
        out.append(f'    <text x="{x+52}" y="{y0+28}" font-size="20" font-weight="900" fill="{color}">{wave}</text>')
        out.append(f'    <text x="{x+52}" y="{y0+48}" font-size="12.5" fill="{MUTED}">{esc(sub)}</text>')
        top = y0 + 70
        out.append(f'    <rect x="{x}" y="{top}" width="{cw}" height="{h - top - 46}" rx="16" fill="{CARD}" stroke="{LINE}"/>')
        for j, (fid, name, eff) in enumerate(items):
            iy = top + 14 + j * (ih + 8)
            out.append(f'    <rect x="{x+12}" y="{iy}" width="{cw-24}" height="{ih}" rx="10" fill="{color}" fill-opacity="0.06" stroke="{color}" stroke-opacity="0.3"/>')
            out.append(f'    <text x="{x+26}" y="{iy+28}" font-size="12" font-weight="800" fill="{color}">{fid}</text>')
            out.append(f'    <text x="{x+72}" y="{iy+28}" font-size="14" font-weight="600" fill="{INK}">{esc(name)}</text>')
            ec = EFFORT[eff]
            out.append(f'    <rect x="{x+cw-50}" y="{iy+12}" width="26" height="22" rx="6" fill="{ec}" fill-opacity="0.12" stroke="{ec}" stroke-opacity="0.6"/>')
            out.append(f'    <text x="{x+cw-37}" y="{iy+28}" font-size="12" font-weight="800" fill="{ec}" text-anchor="middle">{eff}</text>')
    ly = h - 20
    out.append(f'    <text x="{x0}" y="{ly}" font-size="12" fill="{MUTED}">Effort:</text>')
    for k, (eff, lab) in enumerate([("S", "≤ 2 days"), ("M", "≤ 1 week"), ("L", "> 1 week")]):
        lx = x0 + 52 + k * 120
        ec = EFFORT[eff]
        out.append(f'    <rect x="{lx}" y="{ly-15}" width="22" height="20" rx="5" fill="{ec}" fill-opacity="0.12" stroke="{ec}" stroke-opacity="0.6"/><text x="{lx+11}" y="{ly}" font-size="11" font-weight="800" fill="{ec}" text-anchor="middle">{eff}</text><text x="{lx+30}" y="{ly}" font-size="12" fill="{MUTED}">{lab}</text>')
    return frame(w, h, "Roadmap", "The backlog in three waves. Pick from the left; every item has acceptance criteria in FEATURES.md.", "\n".join(out),
                 "Roadmap in three waves: now, next, later")

# ---------------------------------------------------------------- window explainer
def window_explainer():
    w, h = 1200, 470
    X0, X1, YB, YT = 90, 1130, 330, 140
    H = 14  # hours shown
    def hx(t): return X0 + (X1 - X0) * t / H
    def sh(t):
        bump = 50 * math.sin((t - 2) / 9 * math.pi) ** 2 if 2 <= t <= 11 else 0.0
        return 32 + 2 * math.sin(t * 0.8) + bump
    def yy(t): return YB - (YB - YT) * sh(t) / 100
    pts = [(hx(t / 8), yy(t / 8)) for t in range(0, H * 8 + 1)]
    line = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in pts)
    out = []
    out.append(f'    <rect x="40" y="100" width="{w-80}" height="310" rx="16" fill="{CARD}" stroke="{LINE}"/>')
    # max-delay cap shading region beyond cap
    cap, latest, deadline, runtime = 9.0, 11.4, 13.1, 1.0
    out.append(f'    <rect x="{hx(cap):.1f}" y="{YT+8}" width="{hx(latest)-hx(cap):.1f}" height="{YB-YT-8}" fill="#f1f5f9"/>')
    out.append(f'    <path d="{line} L {X1} {YB} L {X0} {YB} Z" fill="#22c55e" fill-opacity="0.12"/>')
    out.append(f'    <path d="{line}" fill="none" stroke="#16a34a" stroke-width="3"/>')
    out.append(f'    <line x1="{X0}" y1="{YB}" x2="{X1}" y2="{YB}" stroke="#94a3b8"/>')
    for t in range(0, H + 1, 2):
        out.append(f'    <text x="{hx(t):.1f}" y="{YB+20}" font-size="11.5" fill="{MUTED}" text-anchor="middle">+{t}h</text>')
    # candidate windows
    for t in [0, 2, 4, 6, 8]:
        out.append(f'    <rect x="{hx(t):.1f}" y="{YB-26}" width="{hx(runtime)-hx(0):.1f}" height="16" rx="4" fill="#cbd5e1" fill-opacity="0.8"/>')
    out.append(f'    <text x="{hx(1):.1f}" y="{YB-34}" font-size="11.5" fill="{MUTED}" text-anchor="middle">candidate windows</text>')
    best = 5.6
    out.append(f'    <rect x="{hx(best):.1f}" y="{yy(best+0.5)-8:.1f}" width="{hx(runtime)-hx(0):.1f}" height="30" rx="7" fill="#16a34a"/>')
    out.append(f'    <text x="{hx(best+0.5):.1f}" y="{yy(best+0.5)+12:.1f}" font-size="12" font-weight="800" fill="#ffffff" text-anchor="middle">chosen</text>')
    out.append(f'    <text x="{hx(best+0.5):.1f}" y="{yy(best+0.5)-18:.1f}" font-size="12" font-weight="700" fill="#15803d" text-anchor="middle">highest mean share</text>')
    def marker(t, label, color, y, sub=None):
        out.append(f'    <line x1="{hx(t):.1f}" y1="{YT+8}" x2="{hx(t):.1f}" y2="{YB}" stroke="{color}" stroke-width="2" stroke-dasharray="5 4"/>')
        out.append(f'    <text x="{hx(t):.1f}" y="{y}" font-size="12.5" font-weight="800" fill="{color}" text-anchor="middle">{esc(label)}</text>')
        if sub:
            out.append(f'    <text x="{hx(t):.1f}" y="{y+15}" font-size="11" fill="{MUTED}" text-anchor="middle">{esc(sub)}</text>')
    marker(0, "submit", "#475569", 126)
    marker(cap, "max-delay cap", "#7c3aed", 126, "optional")
    marker(latest, "latest start", "#d97706", 126)
    marker(deadline, "deadline", "#dc2626", 126)
    # runtime + margin brace
    by = YB + 44
    out.append(f'    <path d="M{hx(latest):.1f} {by} H{hx(deadline):.1f}" stroke="#d97706" stroke-width="2"/>')
    out.append(f'    <text x="{(hx(latest)+hx(deadline))/2:.1f}" y="{by+18}" font-size="11.5" fill="{MUTED}" text-anchor="middle">runtime + safety margin</text>')
    out.append(f'    <text x="{X0}" y="{by+18}" font-size="12" fill="{MUTED}">Search space = [submit, min(latest start, max-delay cap)]. Shaded = excluded by the cap.</text>')
    return frame(w, h, "How the greenest window is chosen",
                 "greenest-window slides a runtime-sized window over the forecast and keeps the one with the highest mean renewable share",
                 "\n".join(out), "Diagram of window selection between submit time and latest start")



# ---------------------------------------------------------------- hero banner
def hero():

    # Renewable share curve over 24h (solar bump + wind)
    def share(h):
        solar = 46 * math.sin((h - 5) / 15 * math.pi) ** 2 if 5 <= h <= 20 else 0
        wind = 30 + 6 * math.sin(h / 24 * 2 * math.pi + 1.2)
        return min(95, wind + solar)

    X0, X1, YB, YT = 660, 1150, 300, 90
    def xy(h):
        x = X0 + (X1 - X0) * h / 24
        y = YB - (YB - YT) * share(h) / 100
        return x, y

    pts = [xy(h / 4) for h in range(0, 97)]
    line = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in pts)
    area = line + f" L {X1} {YB} L {X0} {YB} Z"

    def hx(h): return X0 + (X1 - X0) * h / 24
    peak_h = max(range(0, 97), key=lambda i: share(i / 4)) / 4

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 380" width="1200" height="380" role="img" aria-label="Carbon-aware Kubernetes scheduler: hold flexible jobs and release them in the greenest window">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#052e16"/>
      <stop offset="0.55" stop-color="#14532d"/>
      <stop offset="1" stop-color="#115e59"/>
    </linearGradient>
    <linearGradient id="area" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#4ade80" stop-opacity="0.55"/>
      <stop offset="1" stop-color="#4ade80" stop-opacity="0.04"/>
    </linearGradient>
    <marker id="arr" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto">
      <path d="M0 0 L10 5 L0 10 z" fill="#fde68a"/>
    </marker>
  </defs>
  <rect width="1200" height="380" rx="24" fill="url(#bg)"/>
  <circle cx="1120" cy="-40" r="180" fill="#22c55e" opacity="0.08"/>
  <circle cx="80" cy="400" r="160" fill="#2dd4bf" opacity="0.08"/>

  <g {FONT}>
    <text x="56" y="92" fill="#86efac" font-size="15" font-weight="700" letter-spacing="3">KUBERNETES · BATCH · SUSTAINABILITY</text>
    <text x="56" y="146" fill="#ffffff" font-size="44" font-weight="800">Carbon-Aware</text>
    <text x="56" y="196" fill="#ffffff" font-size="44" font-weight="800">Batch Scheduler</text>
    <text x="56" y="238" fill="#d1fae5" font-size="18">Hold flexible Jobs. Release them when the</text>
    <text x="56" y="262" fill="#d1fae5" font-size="18">German grid is greenest. Never miss a deadline.</text>

    <g font-size="13" font-weight="600">
      <rect x="56" y="292" width="132" height="32" rx="16" fill="#ffffff" fill-opacity="0.12" stroke="#86efac" stroke-opacity="0.5"/>
      <text x="122" y="313" fill="#ecfdf5" text-anchor="middle">kopf controller</text>
      <rect x="198" y="292" width="150" height="32" rx="16" fill="#ffffff" fill-opacity="0.12" stroke="#86efac" stroke-opacity="0.5"/>
      <text x="273" y="313" fill="#ecfdf5" text-anchor="middle">Energy-Charts API</text>
      <rect x="358" y="292" width="176" height="32" rx="16" fill="#ffffff" fill-opacity="0.12" stroke="#86efac" stroke-opacity="0.5"/>
      <text x="446" y="313" fill="#ecfdf5" text-anchor="middle">Prometheus + Grafana</text>
    </g>
  </g>

  <!-- chart panel -->
  <rect x="620" y="40" width="550" height="300" rx="18" fill="#ffffff" fill-opacity="0.06" stroke="#ffffff" stroke-opacity="0.15"/>
  <g {FONT} font-size="12" fill="#a7f3d0">
    <text x="640" y="66" font-weight="700" fill="#ecfdf5" font-size="13">Renewable share forecast (24 h)</text>
    <line x1="{X0}" y1="{YB}" x2="{X1}" y2="{YB}" stroke="#a7f3d0" stroke-opacity="0.4"/>
    <text x="{X0}" y="318">00:00</text>
    <text x="{hx(12):.0f}" y="318" text-anchor="middle">12:00</text>
    <text x="{X1}" y="318" text-anchor="end">24:00</text>
  </g>
  <path d="{area}" fill="url(#area)"/>
  <path d="{line}" fill="none" stroke="#4ade80" stroke-width="3" stroke-linejoin="round"/>

  <!-- submitted job (grey) -->
  <rect x="{hx(2):.1f}" y="244" width="{hx(4)-hx(2):.1f}" height="40" rx="8" fill="#94a3b8" fill-opacity="0.85"/>
  <text x="{hx(3):.1f}" y="269" {FONT} font-size="12" font-weight="700" fill="#0f172a" text-anchor="middle">Job</text>
  <text x="{hx(3):.1f}" y="234" {FONT} font-size="11" fill="#e2e8f0" text-anchor="middle">submitted</text>

  <!-- shift arrow -->
  <path d="M {hx(4)+6:.1f} 262 C {hx(6):.1f} 262, {hx(8):.1f} 262, {hx(peak_h-1)-8:.1f} 262" fill="none" stroke="#fde68a" stroke-width="2.5" stroke-dasharray="6 5" marker-end="url(#arr)"/>
  <text x="{(hx(4)+hx(peak_h-1))/2:.1f}" y="252" {FONT} font-size="12" font-weight="700" fill="#fde68a" text-anchor="middle">time-shift</text>

  <!-- released job (green) -->
  <rect x="{hx(peak_h-1):.1f}" y="244" width="{hx(2)-hx(0):.1f}" height="40" rx="8" fill="#22c55e" stroke="#bbf7d0" stroke-width="2"/>
  <text x="{hx(peak_h):.1f}" y="269" {FONT} font-size="12" font-weight="800" fill="#052e16" text-anchor="middle">Job</text>
  <text x="{hx(peak_h):.1f}" y="234" {FONT} font-size="11" font-weight="700" fill="#ecfdf5" text-anchor="middle">released · greenest window</text>

  <!-- deadline -->
  <line x1="{hx(20):.1f}" y1="80" x2="{hx(20):.1f}" y2="{YB}" stroke="#fca5a5" stroke-width="2" stroke-dasharray="4 4"/>
  <text x="{hx(20):.1f}" y="76" {FONT} font-size="11" font-weight="700" fill="#fca5a5" text-anchor="middle">deadline</text>
</svg>
'''
    return svg


GENERATORS = {
    "hero": hero,
    "how-it-works": how_it_works,
    "feature-map": feature_map,
    "roadmap": roadmap,
    "window-selection": window_explainer,
}

if __name__ == "__main__":
    for name, fn in GENERATORS.items():
        (OUT / f"{name}.svg").write_text(fn(), encoding="utf-8")
        print(f"wrote {name}.svg")
