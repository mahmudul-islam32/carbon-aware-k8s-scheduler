"""A simulated cluster that drives the dashboard without Kubernetes.

``carbon-sim dashboard --demo`` runs :class:`DemoCluster` in a background
thread. It submits realistic batch Jobs on an accelerated clock, plans each one
with the real planner policies against a forecast (synthetic by default, or
the live Energy-Charts forecast), releases them when their window opens and
pushes everything into the same :data:`~carbon_scheduler.dashboard.STATE` the
controller uses. What you see in the demo is what the controller would do.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from carbon_scheduler.carbon.live import JobResources
from carbon_scheduler.dashboard.state import STATE, DashboardState, JobRecord
from carbon_scheduler.planner.models import Forecast
from carbon_scheduler.planner.policies import PolicyName
from carbon_scheduler.sim.plan import plan_job
from carbon_scheduler.sim.synthetic import synthetic_forecast


@dataclass(frozen=True)
class JobTemplate:
    """A kind of batch Job the demo submits."""

    name: str
    namespace: str
    team: str
    runtime: timedelta
    slack: timedelta  # deadline = submit + runtime + slack
    cpu: float
    gpu: float = 0.0
    policy: PolicyName = "greenest-window"
    weight: float = 1.0


TEMPLATES: tuple[JobTemplate, ...] = (
    JobTemplate("nightly-etl", "data", "data-platform", timedelta(hours=2), timedelta(hours=8), 32),
    JobTemplate("resnet-train", "ml", "ml-research", timedelta(hours=4), timedelta(hours=20), 8, 4,
                weight=0.6),
    JobTemplate("llm-finetune", "ml", "ml-research", timedelta(hours=6), timedelta(hours=30), 16, 8,
                weight=0.3),
    JobTemplate("report-gen", "finance", "finance-bi", timedelta(minutes=30), timedelta(hours=4), 2,
                policy="threshold"),
    JobTemplate("video-transcode", "media", "media", timedelta(hours=1), timedelta(hours=6), 64,
                policy="balanced"),
    JobTemplate("db-backup", "platform", "sre", timedelta(minutes=45), timedelta(hours=10), 4),
    JobTemplate("feature-refresh", "ml", "ml-platform", timedelta(hours=1, minutes=30),
                timedelta(hours=12), 24, policy="balanced"),
    JobTemplate("log-compaction", "platform", "sre", timedelta(hours=1), timedelta(hours=16), 6),
    JobTemplate("recsys-retrain", "ml", "ml-platform", timedelta(hours=3), timedelta(hours=14), 8, 2,
                weight=0.7),
    JobTemplate("invoice-batch", "finance", "finance-bi", timedelta(minutes=40),
                timedelta(hours=2), 4, policy="immediate", weight=0.5),
)


@dataclass
class _Held:
    record: JobRecord
    runtime: timedelta


class DemoCluster:
    """Accelerated-time simulation of a cluster running the carbon scheduler.

    Args:
        forecast: Grid forecast to plan against; synthetic if omitted.
        speed: Simulated seconds per real second (1200 = 20 sim-minutes per second).
        seed: Random seed for reproducible demos.
        state: Dashboard state to write to.
        start: Simulated start time (defaults to now, rounded down to the hour).
        mean_gap: Average simulated time between submissions.
        warmup: Simulated history to fill in before the clock starts.
    """

    STEP = timedelta(minutes=5)

    def __init__(
        self,
        forecast: Forecast | None = None,
        speed: float = 1200.0,
        seed: int | None = 7,
        state: DashboardState | None = None,
        start: datetime | None = None,
        mean_gap: timedelta = timedelta(minutes=50),
        warmup: timedelta = timedelta(hours=10),
    ) -> None:
        base = start or datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        self.rng = random.Random(seed)
        self.speed = speed
        self.state = state or STATE
        self.mean_gap = mean_gap
        self.forecast = forecast or synthetic_forecast(
            base - warmup - timedelta(hours=6), hours=24 * 8, seed=seed
        )
        self._t = base - warmup
        self._start_sim = base
        self._held: dict[str, _Held] = {}
        self._counter = 0
        self._next_submit = self._t
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        self.state.reset()
        self.state.clock = self.now
        self.state.configure(mode="demo", policy="mixed (per-job)")
        self.state.set_forecast(self.forecast, fetched_at=self._t)
        self.advance_to(base)

    # ---- clock -----------------------------------------------------------

    def now(self) -> datetime:
        """Current simulated time."""
        return self._t

    def advance_to(self, target: datetime) -> None:
        """Process submissions and releases up to ``target`` in small steps."""
        with self._lock:
            while self._t < target:
                self._t = min(target, self._t + self.STEP)
                self._tick()

    # ---- simulation ------------------------------------------------------

    def _pick_template(self) -> JobTemplate:
        weights = [t.weight for t in TEMPLATES]
        return self.rng.choices(TEMPLATES, weights=weights, k=1)[0]

    def submit(self, template: JobTemplate | None = None) -> JobRecord:
        """Submit one Job now and plan it with its policy."""
        tpl = template or self._pick_template()
        self._counter += 1
        name = f"{tpl.name}-{self._counter:04d}"
        now = self._t
        jitter = self.rng.uniform(0.7, 1.3)
        runtime = timedelta(seconds=tpl.runtime.total_seconds() * jitter)
        deadline = now + runtime + tpl.slack
        result = plan_job(
            forecast=self.forecast,
            now=now,
            runtime=runtime,
            deadline=deadline,
            policy=tpl.policy,
            threshold_pct=55,
            resources=JobResources(cpu_cores=tpl.cpu, gpu_units=tpl.gpu),
        )
        window = result.window
        avoided = result.avoided
        record = JobRecord(
            namespace=tpl.namespace,
            name=name,
            status="held",
            policy=window.policy_used,
            submitted=now,
            planned_start=window.start,
            latest_start=result.latest_start,
            deadline=deadline,
            runtime_seconds=runtime.total_seconds(),
            expected_share_pct=window.mean_renewable_share_pct,
            baseline_share_pct=avoided.baseline_share_pct if avoided else None,
            co2_avoided_g=max(0.0, avoided.avoided_gco2) if avoided else None,
            team=tpl.team,
        )
        self.state.upsert_job(record)

        if window.start <= now + timedelta(minutes=1):
            self.state.mark_released(tpl.namespace, name, at=now)
            self.state.add_event(
                tpl.namespace, name, "JobReleased",
                f"Job released for immediate execution (policy: {window.policy_used})",
            )
        else:
            self._held[record.key] = _Held(record, runtime)
            co2 = f", est. {record.co2_avoided_g:.0f} gCO2 avoided" if record.co2_avoided_g else ""
            self.state.add_event(
                tpl.namespace, name, "JobHeld",
                f"Job held until {window.start:%a %H:%M} UTC (policy: {window.policy_used}{co2})",
            )
        return record

    def _extend_forecast_if_needed(self) -> None:
        """Keep at least two days of forecast ahead of the simulated clock."""
        if not self.forecast.points:
            return
        last = self.forecast.points[-1].timestamp
        if last - self._t > timedelta(hours=48):
            return
        more = synthetic_forecast(
            last + timedelta(minutes=15), hours=24 * 4, seed=self.rng.randint(0, 10**6)
        )
        keep_from = self._t - timedelta(hours=30)
        self.forecast = Forecast(
            points=[p for p in self.forecast.points if p.timestamp >= keep_from] + more.points
        )
        self.state.set_forecast(self.forecast, fetched_at=self._t)

    def _tick(self) -> None:
        now = self._t
        self._extend_forecast_if_needed()
        while now >= self._next_submit:
            self.submit()
            gap = self.rng.expovariate(1 / self.mean_gap.total_seconds())
            self._next_submit += timedelta(seconds=max(300.0, gap))

        for key, held in list(self._held.items()):
            rec = held.record
            if rec.planned_start is not None and now >= rec.planned_start:
                self.state.mark_released(rec.namespace, rec.name, at=now)
                self.state.add_event(
                    rec.namespace, rec.name, "JobReleased", "Job released at planned start time"
                )
                del self._held[key]

    # ---- background thread ----------------------------------------------

    def start(self) -> None:
        """Run the simulation clock in a daemon thread."""
        if self._thread is not None:
            return
        real_start = time.monotonic()
        sim_start = self._t

        def loop() -> None:
            while not self._stop.is_set():
                elapsed = time.monotonic() - real_start
                self.advance_to(sim_start + timedelta(seconds=elapsed * self.speed))
                self._stop.wait(0.25)

        self._thread = threading.Thread(target=loop, daemon=True, name="carbon-demo")
        self._thread.start()

    def stop(self) -> None:
        """Stop the background thread."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
