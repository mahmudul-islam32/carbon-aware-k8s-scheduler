# Alert runbooks

These runbooks belong to the optional `PrometheusRule` alert pack ([F-32](FEATURES.md#f-32--prometheusrule-alert-pack)).
Turn it on with:

```bash
helm upgrade --install carbon-scheduler ./charts/carbon-scheduler \
  --set metrics.serviceMonitor.enabled=true \
  --set alerts.enabled=true \
  --set alerts.labels.release=kube-prometheus-stack   # match your Prometheus ruleSelector
```

Thresholds, windows, `for` durations and severities live under `alerts.*` in
[`values.yaml`](../charts/carbon-scheduler/values.yaml). If other applications in the same Prometheus
also export `fallback_releases_total`, set `alerts.selector` (for example `job="carbon-scheduler-metrics"`)
so the rules only look at this controller.

Every alert fails safe: the scheduler releases Jobs fail-open, so none of these alerts means a Job
will miss its deadline because of the scheduler. They mean CO₂ is not being saved, or the controller
is not doing its job.

| Alert | Default condition | Severity |
|---|---|:---:|
| [`CarbonSchedulerForecastStale`](#carbonschedulerforecaststale) | Forecast older than 2 h at the latest decision, for 10 min | warning |
| [`CarbonSchedulerHighFallbackRate`](#carbonschedulerhighfallbackrate) | > 20 % of releases in 1 h were fallbacks (at least 5 releases), for 5 min | warning |
| [`CarbonSchedulerEnergyChartsErrors`](#carbonschedulerenergychartserrors) | ≥ 3 Energy-Charts 5xx, 429 or network errors in 15 min | warning |
| [`CarbonSchedulerJobHeldTooLong`](#carbonschedulerjobheldtoolong) | A Job is still held 5 min after creation + `max-delay` | critical |

---

### CarbonSchedulerForecastStale

**What it means.** `carbon_forecast_age_seconds` is the age of the renewable-share forecast at the most
recent planning decision. Above 2 h, the controller is planning with old data: the forecast cache
refreshes every `CARBON_FORECAST_REFRESH_MINUTES` (30 min by default), so it has not been able to
refresh several times in a row. The gauge only changes when a Job is planned, so the alert resolves with
the next decision that uses a fresh forecast.

**Check.**
1. `kubectl logs deploy/carbon-scheduler | grep -E 'grid_data_fetch|api_request_error|api_error'`
2. Is `CarbonSchedulerEnergyChartsErrors` firing too? Then the API is the cause; follow that runbook.
3. Can the pod reach the API? `kubectl exec deploy/carbon-scheduler -- python -c "import urllib.request; print(urllib.request.urlopen('https://api.energy-charts.info/ren_share_forecast?country=de').status)"`
   A timeout points at a NetworkPolicy, egress proxy or DNS problem.

**Fix.** Restore egress to `api.energy-charts.info`. If Energy-Charts itself is down there is nothing to
fix locally: Jobs keep running fail-open when no data is available. Restarting the controller does not
help with an API outage.

---

### CarbonSchedulerHighFallbackRate

**What it means.** More than 20 % of Job releases in the last hour were fail-open fallbacks
(`fallback_releases_total` vs `carbon_jobs_released_total`). Jobs still run on time, but they are not
being shifted, so no CO₂ is saved. The rule needs at least `alerts.fallbackRate.minReleases` releases in
the window so a single odd Job does not page anyone.

**Check.** Find out why Jobs fall back:
1. `kubectl get events -A --field-selector reason=FallbackRelease` and `kubectl get events -A --field-selector reason=ValidationError`
2. Fallbacks decided at planning time also carry a `fallback-reason` annotation:
   `kubectl get jobs -A -o json | jq -r '.items[] | .metadata.annotations["carbon-scheduler.mahmudul-islam32.github.io/fallback-reason"] // empty' | sort | uniq -c`

| Signal | Cause | Fix |
|---|---|---|
| `grid_data_unavailable`, `no_forecast_data` | API unreachable or empty | See [`CarbonSchedulerEnergyChartsErrors`](#carbonschedulerenergychartserrors) |
| `forecast_data_degraded` | Energy-Charts serves substitute data (`substitute: true`) | Wait; it is an upstream data gap |
| `ValidationError` Events | Bad annotations on the Jobs | Fix the Job templates; the Event message names the key |
| `current_time_past_latest_feasible_start` | Deadlines too tight for the runtime | Give the Jobs more slack or a smaller `estimated-runtime` |
| `FallbackRelease` Event "released at deadline safety margin" | Controller was down or the timer was late | Check the controller pod's restarts and logs |

**Fix.** Use the "Fix" column for the dominant signal. If it is upstream (API outage or degraded data),
there is nothing to change: Jobs keep running on time, and on-plan shifting resumes once data is back.

**Note.** The rule compares counter sums with their value one window ago, so it cannot see across a
controller restart (counters reset). It stays quiet for one window after a restart.

---

### CarbonSchedulerEnergyChartsErrors

**What it means.** At least 3 requests to the Energy-Charts API failed with an HTTP 5xx, a 429 (rate
limit) or a network error in the last 15 minutes (`carbon_api_requests_total{status=~"5..|429|network_error"}`).

**Check.**
1. Which status? `sum by (status, endpoint) (carbon_api_requests_total{status!="200"})` in Prometheus.
2. **429**: the public API allows roughly 2 requests per minute per endpoint. Are several controllers
   sharing one egress IP (for example several clusters behind one NAT, or more than one replica)? Is
   `CARBON_FORECAST_REFRESH_MINUTES` set very low? The client honours `Retry-After` on its own.
3. **5xx**: check [api.energy-charts.info](https://api.energy-charts.info/) from your workstation. An
   upstream outage cannot be fixed locally.
4. **network_error**: DNS, egress NetworkPolicy, proxy or TLS interception. See step 3 of
   [`CarbonSchedulerForecastStale`](#carbonschedulerforecaststale).

**Fix.** Raise `config.forecastRefreshMinutes`, run a single replica, or restore egress. While the API is
failing, Jobs are released fail-open, so expect `CarbonSchedulerHighFallbackRate` to follow.

---

### CarbonSchedulerJobHeldTooLong

**What it means.** A Job is still suspended although its creation time plus its `max-delay` (the per-job
annotation or `CARBON_MAX_DELAY`) passed more than 5 minutes ago. The controller should have released it
at `latest-start`, so the release loop is stuck or failing. **This one can cost a deadline**, which is why
it is critical. The series is `carbon_job_hold_limit_timestamp_seconds{namespace, job_name}`; it exists
only while the Job is held and a max-delay applies.

**Check.**
1. The Job: `kubectl describe job <job_name> -n <namespace>`. Look at `planned-start`, `latest-start` and
   the Events.
2. The controller: `kubectl get pods -l app.kubernetes.io/name=carbon-scheduler` (restarts, readiness)
   and `kubectl logs deploy/carbon-scheduler | grep -E '<job_name>|patch|Forbidden'`.
3. RBAC: the controller needs `patch` on `jobs`. `kubectl auth can-i patch jobs --as=system:serviceaccount:<controller-namespace>:<service-account> -n <namespace>`
4. Was the Job created while the controller was down? Then it was planned late and `latest-start` is
   after creation + max-delay. The Job is released at `latest-start`; this alert then clears by itself.

**Fix.** Release the Job by hand if its deadline is close:

```bash
kubectl patch job <job_name> -n <namespace> --type merge -p '{"spec":{"suspend":false}}'
```

Then fix the controller (crash loop, RBAC, API server connectivity) so it doesn't happen again.
