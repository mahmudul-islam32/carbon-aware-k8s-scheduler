// Records the README media from the demo dashboard and the HTML report.
//
//   node docs/media/record.mjs            (or: make media)
//
// Needs: carbon-sim on PATH, Playwright with Chromium, ffmpeg.
// Writes into docs/media/: dashboard-light.png, dashboard-dark.png,
// dashboard-mobile.png, report.png, dashboard-demo.mp4, dashboard-demo.gif
import { spawn, execFileSync } from "node:child_process";
import { mkdtempSync, readdirSync, renameSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const OUT = dirname(fileURLToPath(import.meta.url));
const PORT = 8765;
const URL = `http://127.0.0.1:${PORT}/`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitForServer() {
  for (let i = 0; i < 60; i++) {
    try { if ((await fetch(URL + "api/state")).ok) return; } catch {}
    await sleep(250);
  }
  throw new Error("dashboard did not start");
}

async function glide(page, from, to, steps = 40, pause = 18) {
  for (let i = 0; i <= steps; i++) {
    const t = i / steps;
    await page.mouse.move(from[0] + (to[0] - from[0]) * t, from[1] + (to[1] - from[1]) * t);
    await sleep(pause);
  }
}

const server = spawn("carbon-sim", ["dashboard", "--port", String(PORT), "--speed", "1500", "--seed", "3", "--start", "2026-06-15T05:00:00Z"], {
  stdio: "ignore",
});
try {
  await waitForServer();
  const browser = await chromium.launch();

  // ---- still screenshots
  for (const theme of ["light", "dark"]) {
    const page = await browser.newPage({ viewport: { width: 1400, height: 1000 }, deviceScaleFactor: 2 });
    await page.goto(`${URL}?theme=${theme}`);
    await sleep(2500);
    await page.screenshot({ path: join(OUT, `dashboard-${theme}.png`), fullPage: true });
    await page.close();
  }
  const mobile = await browser.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
  await mobile.goto(`${URL}?theme=light`);
  await sleep(2000);
  await mobile.screenshot({ path: join(OUT, "dashboard-mobile.png") });
  await mobile.close();

  // ---- video
  const videoDir = mkdtempSync(join(tmpdir(), "carbon-video-"));
  const ctx = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    recordVideo: { dir: videoDir, size: { width: 1280, height: 800 } },
  });
  const page = await ctx.newPage();
  await page.goto(`${URL}?theme=light`);
  await sleep(1800);
  const chart = await page.locator("#timeline").boundingBox();
  const y = chart.y + 110;
  await glide(page, [chart.x + 60, y], [chart.x + chart.width - 30, y], 70, 30);
  // hover a few job lanes
  const lanes = page.locator("#timeline g.lane rect:nth-child(3)");
  const n = await lanes.count();
  for (let i = Math.max(0, n - 4); i < n; i++) {
    await lanes.nth(i).hover();
    await sleep(700);
  }
  await page.mouse.wheel(0, 640);
  await sleep(900);
  const heat = await page.locator("#heat").boundingBox();
  if (heat) await glide(page, [heat.x + 50, heat.y + 30], [heat.x + heat.width - 20, heat.y + 60], 30, 30);
  const teams = await page.locator("#teams").boundingBox();
  if (teams) { await page.mouse.move(teams.x + 140, teams.y + 18); await sleep(800); }
  await page.mouse.wheel(0, 500);
  await sleep(1500);
  await page.mouse.wheel(0, -1200);
  await sleep(600);
  await page.click("#themeBtn");
  await sleep(2500);
  const c2 = await page.locator("#timeline").boundingBox();
  await glide(page, [c2.x + c2.width - 30, c2.y + 110], [c2.x + 80, c2.y + 140], 50, 30);
  await sleep(800);
  await ctx.close();
  const webm = readdirSync(videoDir).find((f) => f.endsWith(".webm"));
  const raw = join(videoDir, webm);

  // ---- report screenshot
  const work = mkdtempSync(join(tmpdir(), "carbon-showcase-"));
  execFileSync("carbon-sim", ["showcase", "--output", work, "--days", "30", "--jobs", "300"], { stdio: "ignore" });
  const rp = await browser.newPage({ viewport: { width: 1200, height: 900 }, deviceScaleFactor: 2 });
  await rp.goto(`file://${join(work, "report.html")}?theme=light`);
  await rp.screenshot({ path: join(OUT, "report.png"), fullPage: true });
  await browser.close();
  rmSync(work, { recursive: true, force: true });

  // ---- encode
  const mp4 = join(OUT, "dashboard-demo.mp4");
  execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-i", raw, "-c:v", "libx264", "-pix_fmt", "yuv420p",
    "-crf", "26", "-preset", "slow", "-movflags", "+faststart", "-an", mp4]);
  const palette = join(videoDir, "palette.png");
  const filters = "fps=10,scale=900:-1:flags=lanczos";
  execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-i", raw, "-vf", `${filters},palettegen=max_colors=128:stats_mode=diff`, palette]);
  execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-i", raw, "-i", palette, "-lavfi",
    `${filters}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle`, join(OUT, "dashboard-demo.gif")]);
  rmSync(videoDir, { recursive: true, force: true });
  console.log("media written to", OUT);
} finally {
  server.kill();
}
