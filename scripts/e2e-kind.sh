#!/usr/bin/env bash
# End-to-end test on a kind cluster (F-33).
#
#   build image -> create kind cluster -> deploy mock Energy-Charts API ->
#   helm install -> wait for /readyz -> apply a flexible Job ->
#   assert planned-start + JobHeld Event -> assert release + JobReleased Event
#
# Exits non-zero on the first failed assertion and dumps controller logs.
# Environment knobs:
#   CLUSTER          kind cluster name              (default carbon-e2e)
#   IMAGE            controller image to build      (default carbon-scheduler:e2e)
#   KIND_NODE_IMAGE  node image for kind            (default: kind's own default)
#   SKIP_BUILD=1     reuse an already built IMAGE
#   KEEP_CLUSTER=1   do not delete the cluster at the end
#   TIMEOUT          seconds per wait               (default 240)
set -euo pipefail

CLUSTER="${CLUSTER:-carbon-e2e}"
IMAGE="${IMAGE:-carbon-scheduler:e2e}"
RELEASE="${RELEASE:-carbon-scheduler}"
NAMESPACE="${NAMESPACE:-carbon-system}"
JOB_NAMESPACE="e2e"
JOB_NAME="e2e-flexible"
WORKLOAD_IMAGE="busybox:1.36"
TIMEOUT="${TIMEOUT:-240}"
PREFIX="carbon-scheduler.mahmudul-islam32.github.io"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

need() { command -v "$1" >/dev/null || { echo "missing: $1" >&2; exit 1; }; }
for bin in docker kind kubectl helm; do need "$bin"; done

step() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31mFAIL: %s\033[0m\n' "$*" >&2; exit 1; }

dump() {
  local rc=$?
  if [[ $rc -ne 0 ]]; then
    step "Diagnostics (exit $rc)"
    kubectl get pods,jobs,events -A || true
    kubectl -n "$NAMESPACE" logs "deploy/$RELEASE" --tail=200 || true
    kubectl -n "$NAMESPACE" logs deploy/mock-energy-charts --tail=50 || true
    kubectl -n "$JOB_NAMESPACE" describe job "$JOB_NAME" || true
  fi
  if [[ "${KEEP_CLUSTER:-0}" != "1" ]]; then
    kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
  fi
  exit $rc
}
trap dump EXIT

# wait_for <description> <command...>: retry the command every 2 s until it succeeds.
wait_for() {
  local what="$1"; shift
  local deadline=$((SECONDS + TIMEOUT))
  until "$@" >/dev/null 2>&1; do
    (( SECONDS < deadline )) || fail "timed out after ${TIMEOUT}s waiting for: $what"
    sleep 2
  done
  echo "ok: $what"
}

annotation() {
  kubectl -n "$JOB_NAMESPACE" get job "$JOB_NAME" \
    -o "jsonpath={.metadata.annotations.${PREFIX//./\\.}/$1}"
}
has_annotation() { [[ -n "$(annotation "$1")" ]]; }
is_released() { [[ "$(kubectl -n "$JOB_NAMESPACE" get job "$JOB_NAME" -o 'jsonpath={.spec.suspend}')" == "false" ]]; }
is_complete() { [[ "$(kubectl -n "$JOB_NAMESPACE" get job "$JOB_NAME" -o 'jsonpath={.status.succeeded}')" == "1" ]]; }
describe_has() { kubectl -n "$JOB_NAMESPACE" describe job "$JOB_NAME" | grep -q "$1"; }
readyz() {
  kubectl get --raw "/api/v1/namespaces/$NAMESPACE/services/http:$RELEASE-metrics:9090/proxy/readyz"
}

if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
  step "Building $IMAGE"
  docker build -t "$IMAGE" "$ROOT"
fi
docker image inspect "$WORKLOAD_IMAGE" >/dev/null 2>&1 || docker pull "$WORKLOAD_IMAGE"

step "Creating kind cluster '$CLUSTER'"
kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
kind create cluster --name "$CLUSTER" ${KIND_NODE_IMAGE:+--image "$KIND_NODE_IMAGE"} --wait 120s
kind load docker-image "$IMAGE" "$WORKLOAD_IMAGE" --name "$CLUSTER"

step "Deploying the mock Energy-Charts API"
kubectl create namespace "$NAMESPACE"
sed "s#IMAGE_PLACEHOLDER#$IMAGE#" "$ROOT/tests/e2e/mock-energy-charts.yaml" \
  | kubectl -n "$NAMESPACE" apply -f -
kubectl -n "$NAMESPACE" rollout status deploy/mock-energy-charts --timeout="${TIMEOUT}s"

step "Installing the Helm chart"
helm install "$RELEASE" "$ROOT/charts/carbon-scheduler" \
  --namespace "$NAMESPACE" \
  --set image.repository="${IMAGE%%:*}" --set image.tag="${IMAGE##*:}" \
  --set image.pullPolicy=Never \
  --set config.gridApiBaseUrl="http://mock-energy-charts.$NAMESPACE.svc.cluster.local:8080" \
  --set config.reconcileIntervalSeconds=10 \
  --set config.logFormat=console \
  --wait --timeout "${TIMEOUT}s"

step "Waiting for /readyz"
wait_for "controller /readyz" readyz
echo "readyz: $(readyz)"

step "Applying the flexible Job"
kubectl create namespace "$JOB_NAMESPACE"
kubectl apply -f "$ROOT/tests/e2e/job.yaml"

step "Asserting the Job is held with a planned start"
wait_for "planned-start annotation" has_annotation planned-start
echo "planned-start: $(annotation planned-start)"
echo "latest-start:  $(annotation latest-start)"
is_released && fail "Job was released before its planned start"
wait_for "JobHeld Event in kubectl describe" describe_has JobHeld

step "Asserting the Job is released at its planned start"
wait_for "spec.suspend=false" is_released
wait_for "actual-start annotation" has_annotation actual-start
echo "actual-start:  $(annotation actual-start)"
wait_for "JobReleased Event in kubectl describe" describe_has JobReleased
fallback="$(annotation fallback-reason)"
[[ -z "$fallback" ]] || fail "Job released by fallback ($fallback), not by the plan"
wait_for "Job completion" is_complete

kubectl -n "$JOB_NAMESPACE" describe job "$JOB_NAME" | sed -n '/^Events:/,$p'
step "E2E passed"
