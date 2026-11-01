#!/usr/bin/env bash
# End-to-end demo on a local kind cluster:
#   build image -> create cluster -> install chart -> submit example Jobs ->
#   port-forward the dashboard to http://localhost:9090
set -euo pipefail

CLUSTER="${CLUSTER:-carbon}"
IMAGE="${IMAGE:-carbon-scheduler:dev}"
RELEASE="${RELEASE:-carbon-scheduler}"
NAMESPACE="${NAMESPACE:-carbon-system}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

need() { command -v "$1" >/dev/null || { echo "missing: $1" >&2; exit 1; }; }
for bin in docker kind kubectl helm; do need "$bin"; done

step() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }

step "Building $IMAGE"
docker build -t "$IMAGE" "$ROOT"

if ! kind get clusters | grep -qx "$CLUSTER"; then
  step "Creating kind cluster '$CLUSTER'"
  kind create cluster --name "$CLUSTER"
fi
kind load docker-image "$IMAGE" --name "$CLUSTER"

step "Installing the Helm chart"
helm upgrade --install "$RELEASE" "$ROOT/charts/carbon-scheduler" \
  --namespace "$NAMESPACE" --create-namespace \
  --set image.repository="${IMAGE%%:*}" --set image.tag="${IMAGE##*:}" \
  --set image.pullPolicy=Never --set config.logFormat=console --wait

step "Submitting example Jobs"
for ns in data ml; do kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f -; done
DEADLINE="$(date -u -d '+10 hours' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v+10H +%Y-%m-%dT%H:%M:%SZ)"
kubectl apply -f - <<YAML
apiVersion: batch/v1
kind: Job
metadata:
  name: nightly-etl
  namespace: data
  labels: { team: data-platform }
  annotations:
    carbon-scheduler.mahmudul-islam32.github.io/flexible: "true"
    carbon-scheduler.mahmudul-islam32.github.io/deadline: "$DEADLINE"
    carbon-scheduler.mahmudul-islam32.github.io/estimated-runtime: "2h"
spec:
  suspend: true
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: etl
          image: busybox:1.36
          command: ["sh", "-c", "echo running && sleep 30"]
          resources: { requests: { cpu: "100m" } }
YAML
kubectl apply -f "$ROOT/examples/balanced-policy.yaml" || true

sleep 5
step "What the controller decided"
kubectl get jobs -A -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,SUSPENDED:.spec.suspend,PLANNED:.metadata.annotations.carbon-scheduler\.mahmudul-islam32\.github\.io/planned-start'

step "Dashboard: http://localhost:9090  (Ctrl+C to stop)"
kubectl -n "$NAMESPACE" port-forward "svc/$RELEASE-metrics" 9090:9090
