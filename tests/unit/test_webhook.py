"""Unit tests for the mutating admission webhook (F-21)."""

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import kopf
import pytest
import yaml
from kopf._cogs.structs import ephemera, references
from kopf._core.engines import admission, indexing
from kopf._core.intents import registries
from kopf._core.reactor import inventory

from carbon_scheduler.config import ANNOTATION_FLEXIBLE, Settings
from carbon_scheduler.controller.webhook import (
    NAMESPACE_OPT_IN_LABEL,
    WEBHOOK_HANDLER_ID,
    WEBHOOK_PATH,
    configure_admission,
    mutate_job,
    needs_suspend,
    on_job_mutate,
    register_webhook,
)

CHART = Path(__file__).resolve().parents[2] / "charts" / "carbon-scheduler"
JOBS = references.Resource(
    group="batch",
    version="v1",
    plural="jobs",
    kind="Job",
    singular="job",
    namespaced=True,
    verbs=["create", "get", "list", "watch", "patch"],
)
FLEXIBLE = {ANNOTATION_FLEXIBLE: "true"}


def _review(annotations: dict[str, str], spec: dict[str, Any], dry_run: bool = False) -> Any:
    return {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "request": {
            "uid": "req-1",
            "kind": {"group": "batch", "version": "v1", "kind": "Job"},
            "resource": {"group": "batch", "version": "v1", "resource": "jobs"},
            "operation": "CREATE",
            "dryRun": dry_run,
            "userInfo": {"username": "alice"},
            "object": {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": "etl", "namespace": "data", "annotations": annotations},
                "spec": spec,
            },
        },
    }


async def _admit(review: Any) -> dict[str, Any]:
    """Run an AdmissionReview through kopf's admission engine with our handler."""
    registry = registries.OperatorRegistry()
    register_webhook(registry)
    insights = references.Insights()
    insights.webhook_resources.add(JOBS)
    response = await admission.serve_admission_request(
        review,
        webhook=WEBHOOK_HANDLER_ID,
        settings=kopf.OperatorSettings(),
        memories=inventory.ResourceMemories(),
        memobase=ephemera.Memo(),
        registry=registry,
        insights=insights,
        indices=indexing.OperatorIndexers().indices,
    )
    result: dict[str, Any] = dict(response["response"])
    return result


def _json_patch(response: dict[str, Any]) -> list[dict[str, Any]]:
    if "patch" not in response:
        return []
    patch: list[dict[str, Any]] = json.loads(base64.b64decode(response["patch"]))
    return patch


class TestSuspendDecision:
    """Criterion: the webhook sets spec.suspend=true when flexible="true" is present."""

    def test_flexible_job_without_suspend_needs_it(self) -> None:
        assert needs_suspend({"template": {}}, FLEXIBLE)

    def test_flexible_job_with_suspend_false_is_overridden(self) -> None:
        assert needs_suspend({"suspend": False}, FLEXIBLE)

    def test_already_suspended_job_is_left_alone(self) -> None:
        assert not needs_suspend({"suspend": True}, FLEXIBLE)

    @pytest.mark.parametrize("annotations", [None, {}, {ANNOTATION_FLEXIBLE: "false"}])
    def test_non_flexible_jobs_are_ignored(self, annotations: dict[str, str] | None) -> None:
        assert not needs_suspend({}, annotations)

    def test_mutate_job_writes_suspend_into_patch(self) -> None:
        body = kopf.Body({"metadata": {"annotations": FLEXIBLE}, "spec": {}})
        patch = kopf.Patch(body=body)
        assert mutate_job(body.spec, body.meta.annotations, patch)
        assert patch.as_json_patch() == [{"op": "add", "path": "/spec/suspend", "value": True}]

    def test_mutate_job_leaves_patch_empty_for_plain_jobs(self) -> None:
        body = kopf.Body({"metadata": {}, "spec": {}})
        patch = kopf.Patch(body=body)
        assert not mutate_job(body.spec, body.meta.annotations, patch)
        assert patch.as_json_patch() == []

    async def test_handler_mutates_via_kopf_objects(self) -> None:
        body = kopf.Body({"metadata": {"name": "etl", "annotations": FLEXIBLE}, "spec": {}})
        patch = kopf.Patch(body=body)
        await on_job_mutate(spec=body.spec, meta=body.meta, patch=patch, name="etl")
        assert patch["spec"]["suspend"] is True


class TestAdmissionEngine:
    """The handler wired into kopf's admission engine returns the right JSON patch."""

    async def test_flexible_job_gets_suspend_patch(self) -> None:
        response = await _admit(_review(FLEXIBLE, {"template": {}}))
        assert response["allowed"] is True
        assert _json_patch(response) == [{"op": "add", "path": "/spec/suspend", "value": True}]

    async def test_suspend_false_is_replaced(self) -> None:
        response = await _admit(_review(FLEXIBLE, {"suspend": False}))
        assert _json_patch(response) == [
            {"op": "replace", "path": "/spec/suspend", "value": True}
        ]

    async def test_plain_job_is_allowed_unchanged(self) -> None:
        response = await _admit(_review({}, {"template": {}}))
        assert response["allowed"] is True
        assert _json_patch(response) == []

    async def test_dry_run_still_returns_patch(self) -> None:
        response = await _admit(_review(FLEXIBLE, {}, dry_run=True))
        assert _json_patch(response)[0]["path"] == "/spec/suspend"


class TestRegistration:
    """Criterion: failurePolicy Ignore; only CREATE is intercepted."""

    def test_generated_webhook_config_fails_open(self) -> None:
        registry = registries.OperatorRegistry()
        register_webhook(registry)
        webhooks = admission.build_webhooks(
            registry._webhooks.get_all_handlers(),
            resources=[JOBS],
            name_suffix="test",
            client_config={"service": {"name": "svc", "namespace": "ns"}},
        )
        assert len(webhooks) == 1
        hook = webhooks[0]
        assert hook["failurePolicy"] == "Ignore"
        assert hook["sideEffects"] == "None"
        assert hook["rules"][0]["operations"] == ["CREATE"]
        assert hook["clientConfig"]["service"]["path"] == WEBHOOK_PATH

    def test_configure_admission_disabled_by_default(self) -> None:
        operator_settings = kopf.OperatorSettings()
        assert not configure_admission(Settings(), operator_settings)
        assert operator_settings.admission.server is None

    def test_configure_admission_uses_mounted_certificate(self, tmp_path: Path) -> None:
        operator_settings = kopf.OperatorSettings()
        settings = Settings(webhook_enabled=True, webhook_port=9443, webhook_cert_dir=tmp_path)
        assert configure_admission(settings, operator_settings)
        server = operator_settings.admission.server
        assert isinstance(server, kopf.WebhookServer)
        assert server.port == 9443
        assert server.addr == "0.0.0.0"
        assert server.certfile == str(tmp_path / "tls.crt")
        assert server.pkeyfile == str(tmp_path / "tls.key")
        # The chart owns the MutatingWebhookConfiguration.
        assert operator_settings.admission.managed is None

    def test_settings_read_from_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CARBON_WEBHOOK_ENABLED", "true")
        monkeypatch.setenv("CARBON_WEBHOOK_PORT", "9443")
        monkeypatch.setenv("CARBON_WEBHOOK_CERT_DIR", "/tls")
        settings = Settings()
        assert settings.webhook_enabled
        assert settings.webhook_port == 9443
        assert settings.webhook_cert_dir == Path("/tls")


HELM = os.environ.get("HELM") or shutil.which("helm")


def _render(*sets: str) -> list[dict[str, Any]]:
    assert HELM is not None
    args = [HELM, "template", "cs", str(CHART), "--kube-version", "1.30.0"]
    for value in sets:
        args += ["--set", value]
    out = subprocess.run(args, check=True, capture_output=True, text=True).stdout
    return [doc for doc in yaml.safe_load_all(out) if doc]


def _kind(docs: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [doc for doc in docs if doc["kind"] == kind]


@pytest.mark.skipif(HELM is None, reason="helm not installed")
class TestHelmChart:
    """Criteria: namespace opt-in, failurePolicy Ignore and TLS in the chart."""

    def test_disabled_by_default(self) -> None:
        docs = _render()
        assert not _kind(docs, "MutatingWebhookConfiguration")
        env = _kind(docs, "Deployment")[0]["spec"]["template"]["spec"]["containers"][0]["env"]
        assert {"name": "CARBON_WEBHOOK_ENABLED", "value": "false"} in env

    def test_webhook_fails_open_and_targets_handler_path(self) -> None:
        docs = _render("webhook.enabled=true")
        hook = _kind(docs, "MutatingWebhookConfiguration")[0]["webhooks"][0]
        assert hook["failurePolicy"] == "Ignore"
        assert hook["rules"][0]["operations"] == ["CREATE"]
        assert hook["clientConfig"]["service"]["path"] == WEBHOOK_PATH
        assert hook["clientConfig"]["service"]["name"] == "cs-carbon-scheduler-webhook"
        # Without opt-in, every namespace except the excluded ones is covered.
        assert "matchLabels" not in hook["namespaceSelector"]
        assert hook["namespaceSelector"]["matchExpressions"][0]["values"] == ["kube-system"]

    def test_namespace_opt_in_label(self) -> None:
        docs = _render("webhook.enabled=true", "webhook.namespaceOptIn=true")
        hook = _kind(docs, "MutatingWebhookConfiguration")[0]["webhooks"][0]
        assert hook["namespaceSelector"]["matchLabels"] == {NAMESPACE_OPT_IN_LABEL: "true"}

    def test_self_signed_bootstrap(self) -> None:
        docs = _render("webhook.enabled=true")
        secret = _kind(docs, "Secret")[0]
        assert secret["type"] == "kubernetes.io/tls"
        assert set(secret["data"]) == {"ca.crt", "tls.crt", "tls.key"}
        hook = _kind(docs, "MutatingWebhookConfiguration")[0]["webhooks"][0]
        assert hook["clientConfig"]["caBundle"] == secret["data"]["ca.crt"]
        deployment = _kind(docs, "Deployment")[0]["spec"]["template"]["spec"]
        assert {"name": "webhook-tls", "secret": {"secretName": secret["metadata"]["name"]}} in (
            deployment["volumes"]
        )
        ports = deployment["containers"][0]["ports"]
        assert {"name": "webhook", "containerPort": 8443, "protocol": "TCP"} in ports

    def test_cert_manager_mode(self) -> None:
        docs = _render("webhook.enabled=true", "webhook.tls.mode=certManager")
        assert not _kind(docs, "Secret")
        cert = _kind(docs, "Certificate")[0]
        assert cert["spec"]["secretName"] == "cs-carbon-scheduler-webhook-tls"
        assert "cs-carbon-scheduler-webhook.default.svc" in cert["spec"]["dnsNames"]
        assert cert["spec"]["issuerRef"]["kind"] == "Issuer"
        assert _kind(docs, "Issuer")
        config = _kind(docs, "MutatingWebhookConfiguration")[0]
        assert config["metadata"]["annotations"]["cert-manager.io/inject-ca-from"] == (
            "default/cs-carbon-scheduler-webhook"
        )
        assert "caBundle" not in config["webhooks"][0]["clientConfig"]

    def test_cert_manager_existing_issuer(self) -> None:
        docs = _render(
            "webhook.enabled=true",
            "webhook.tls.mode=certManager",
            "webhook.tls.certManager.issuerRef.kind=ClusterIssuer",
            "webhook.tls.certManager.issuerRef.name=corp-ca",
        )
        assert not _kind(docs, "Issuer")
        cert = _kind(docs, "Certificate")[0]
        assert cert["spec"]["issuerRef"] == {"kind": "ClusterIssuer", "name": "corp-ca"}
