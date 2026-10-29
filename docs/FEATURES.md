# Feature Specification: carbon-aware-k8s-scheduler

All features are **Planned**. Priority uses MoSCoW: Must, Should, Could.

| ID | Feature | Priority | Status |
|---|---|---|---|
| F-01 | Flexible-job annotation contract | Must | Planned |
| F-02 | Suspend and release controller | Must | Planned |
| F-03 | Energy-Charts grid client | Must | Planned |
| F-04 | Window-selection policies | Must | Planned |
| F-05 | Deadline guarantee and fail-open fallback | Must | Planned |
| F-06 | CO₂ estimation and counterfactual | Must | Planned |
| F-07 | Prometheus metrics | Must | Planned |
| F-08 | Grafana dashboard | Should | Planned |
| F-09 | Backtest and simulation CLI | Should | Planned |
| F-10 | Helm chart and RBAC | Should | Planned |
| F-11 | KEDA ScaledJob mode | Could | Planned |
| F-12 | Kubernetes Events and audit trail | Could | Planned |

---

### F-01: Flexible-job annotation contract
**Description:** A documented set of annotations on `batch/v1` Jobs that declares a job flexible, with a deadline and an estimated runtime.
**User story:** As a data engineer, I want to mark my nightly ETL Job as flexible with one annotation, so that I don't need to learn a new CRD.
**Acceptance criteria:**
- Jobs without `flexible: "true"` are ignored.
- Invalid deadline or runtime values cause a Warning Event, and the Job is released right away (fail-open).
- The contract is documented with examples.
**Priority:** Must · **Status:** Planned

### F-02: Suspend and release controller
**Description:** Watches annotated Jobs submitted with `spec.suspend: true` and patches `spec.suspend: false` at the planned start time.
**User story:** As a platform engineer, I want delayed Jobs to use native Kubernetes behaviour, so that existing tooling keeps working.
**Acceptance criteria:**
- The planned start time is written to an annotation (`.../planned-start`).
- The Job is released within 60 s of the planned start.
- A controller restart does not lose or double-release Jobs, because state is rebuilt from annotations.
**Priority:** Must · **Status:** Planned

### F-03: Energy-Charts grid client
**Description:** A Python client for `GET /ren_share_forecast`, `/signal` and `/public_power` (`country=de`) at `https://api.energy-charts.info`, with response caching.
**User story:** As the controller, I need an up-to-date renewable-share forecast without exceeding the public rate limits.
**Acceptance criteria:**
- Handles HTTP 429 by honouring `Retry-After`.
- Caches responses, refreshing every 30 minutes by default.
- Flags the data as degraded when the `/signal` or `/ren_share_forecast` response has `substitute: true`.
- Unit tests use recorded fixtures, not live calls.
**Priority:** Must · **Status:** Planned

### F-04: Window-selection policies
**Description:** Pluggable policies: `immediate` (baseline), `threshold` (first slot with share ≥ X %) and `greenest-window` (the contiguous window of estimated-runtime length with the highest mean forecast renewable share that ends before the deadline).
**User story:** As a researcher, I want to compare policies on the same input, so that I can quantify the benefit of each.
**Acceptance criteria:**
- Policies are pure functions of the forecast, the job and the current time, with unit tests.
- The chosen window never ends after the deadline.
**Priority:** Must · **Status:** Planned

### F-05: Deadline guarantee and fail-open fallback
**Description:** Release at the latest feasible start (deadline minus runtime minus a safety margin), and release immediately if grid data is unavailable.
**User story:** As a job owner, I want my job to finish on time even when the forecast service is down.
**Acceptance criteria:**
- In simulated API outage tests, no Job misses its deadline because of the scheduler.
- A fallback release increments the `fallback_releases_total` metric.
**Priority:** Must · **Status:** Planned

### F-06: CO₂ estimation and counterfactual
**Description:** Estimates grid carbon intensity per interval from `/public_power` production per type multiplied by configurable emission factors. Job energy is estimated as requested CPU/GPU × a configured power per unit × runtime. Avoided CO₂ = emissions(start at submit) − emissions(actual start).
**User story:** As a sustainability lead, I want to see estimated kg CO₂ avoided per job, so that I can report savings.
**Acceptance criteria:**
- Emission factors and their sources live in a versioned YAML file.
- Each output is clearly labelled an estimate.
- The calculation is unit-tested with hand-computed examples.
**Priority:** Must · **Status:** Planned

### F-07: Prometheus metrics
**Description:** Exposes `carbon_jobs_held`, `carbon_job_delay_seconds` (histogram), `carbon_co2_avoided_grams_total`, `carbon_grid_renewable_share_percent` and `fallback_releases_total`.
**User story:** As an SRE, I want standard metrics, so that I can alert on held Jobs and data outages.
**Acceptance criteria:** Metrics follow Prometheus naming conventions and are scrapeable at `/metrics`.
**Priority:** Must · **Status:** Planned

### F-08: Grafana dashboard
**Description:** A provisioned dashboard showing the renewable share forecast vs actual, held jobs, delays and cumulative CO₂ avoided vs the immediate baseline.
**User story:** As a stakeholder, I want one screen that shows whether the scheduler is worth it.
**Acceptance criteria:** The dashboard JSON ships with the Helm chart and loads without manual edits.
**Priority:** Should · **Status:** Planned

### F-09: Backtest and simulation CLI
**Description:** `carbon-sim` replays historical Energy-Charts data (fetched once and stored locally) against a synthetic or recorded job trace.
**User story:** As a researcher, I want offline experiments that are reproducible and don't depend on live data.
**Acceptance criteria:** The CLI outputs a CSV and a summary table per policy (avoided CO₂, mean delay, deadline misses), and is deterministic for a fixed seed.
**Priority:** Should · **Status:** Planned

### F-10: Helm chart and RBAC
**Description:** A chart with a Deployment, ServiceAccount, least-privilege Role (get/list/watch/patch on Jobs, create Events) and a ServiceMonitor (optional).
**User story:** As a platform engineer, I want to install the scheduler with one command.
**Acceptance criteria:** `helm lint` passes, and the chart installs on kind in CI.
**Priority:** Should · **Status:** Planned

### F-11: KEDA ScaledJob mode
**Description:** An alternative design where work items are queued and a KEDA `ScaledJob` scales using the Metrics API scaler, which points at a controller endpoint returning "allowed concurrency" based on grid greenness.
**User story:** As a team already using KEDA, I want a queue-driven variant.
**Acceptance criteria:** A documented example works on kind with KEDA installed.
**Priority:** Could · **Status:** Planned

### F-12: Kubernetes Events and audit trail
**Description:** Emits Events for held, planned, released and fallback actions.
**User story:** As a job owner, I want `kubectl describe job` to explain why my job hasn't started.
**Acceptance criteria:** Each state change produces exactly one Event with a reason code.
**Priority:** Could · **Status:** Planned
