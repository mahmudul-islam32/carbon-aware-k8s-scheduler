"""Mutating admission webhook that suspends flexible Jobs (F-21).

Users only have to add the ``flexible: "true"`` annotation: on Job CREATE the
webhook sets ``spec.suspend: true`` so the controller can hold the Job until its
planned start. Forgetting ``suspend: true`` no longer means the Job runs at once.

The webhook is served by kopf's built-in HTTPS server on ``CARBON_WEBHOOK_PORT``
with the certificate mounted at ``CARBON_WEBHOOK_CERT_DIR``. The
``MutatingWebhookConfiguration`` itself is owned by the Helm chart (with
``failurePolicy: Ignore``, an optional namespace-label opt-in and TLS from
cert-manager or a self-signed bootstrap), so kopf never edits cluster-wide
webhook objects.
"""

from collections.abc import Mapping
from typing import Any

import kopf
import structlog

from carbon_scheduler.config import Settings
from carbon_scheduler.controller.annotations import is_flexible_job

logger = structlog.get_logger(__name__)

# The webhook URL path is "/<handler id>"; the Helm chart points the service path here.
WEBHOOK_HANDLER_ID = "suspend-flexible-jobs"
WEBHOOK_PATH = f"/{WEBHOOK_HANDLER_ID}"
# Namespace label for the optional opt-in (rendered into the chart's namespaceSelector).
NAMESPACE_OPT_IN_LABEL = "carbon-scheduler/enabled"


def needs_suspend(spec: Mapping[str, Any], annotations: Mapping[str, str] | None) -> bool:
    """Whether the webhook must set ``spec.suspend: true`` on this Job.

    Args:
        spec: Job spec from the admission request
        annotations: Job annotations from the admission request

    Returns:
        True for flexible Jobs that are not already suspended
    """
    if not is_flexible_job(dict(annotations or {})):
        return False
    return spec.get("suspend") is not True


def mutate_job(
    spec: Mapping[str, Any],
    annotations: Mapping[str, str] | None,
    patch: kopf.Patch,
) -> bool:
    """Add ``spec.suspend: true`` to the admission patch when needed.

    Args:
        spec: Job spec from the admission request
        annotations: Job annotations from the admission request
        patch: kopf patch that becomes the admission response's JSON patch

    Returns:
        True if the Job was mutated
    """
    if not needs_suspend(spec, annotations):
        return False
    patch.spec["suspend"] = True
    return True


async def on_job_mutate(
    spec: kopf.Spec,
    meta: kopf.Meta,
    patch: kopf.Patch,
    dryrun: bool = False,
    name: str | None = None,
    namespace: str | None = None,
    **_kwargs: Any,
) -> None:
    """kopf mutating handler for Job CREATE requests."""
    mutated = mutate_job(spec, meta.get("annotations"), patch)
    if mutated:
        logger.info(
            "webhook_suspended_job",
            job_name=name or meta.get("generateName"),
            namespace=namespace,
            dry_run=dryrun,
        )


def register_webhook(registry: kopf.OperatorRegistry | None = None) -> None:
    """Register the mutating handler with kopf.

    Only called when the webhook is enabled: kopf refuses to start when admission
    handlers exist but no webhook server is configured.

    Args:
        registry: kopf registry (defaults to the global one)
    """
    kopf.on.mutate(
        "batch",
        "v1",
        "jobs",
        id=WEBHOOK_HANDLER_ID,
        operations=["CREATE"],
        ignore_failures=True,
        side_effects=False,
        registry=registry,
    )(on_job_mutate)


def build_webhook_server(settings: Settings) -> kopf.WebhookServer:
    """Build kopf's HTTPS webhook server from the mounted certificate.

    kopf re-reads the certificate files every minute, so cert-manager renewals
    are picked up without a restart.

    Args:
        settings: Application settings

    Returns:
        Configured webhook server
    """
    cert_dir = settings.webhook_cert_dir
    return kopf.WebhookServer(
        addr="0.0.0.0",
        port=settings.webhook_port,
        certfile=str(cert_dir / "tls.crt"),
        pkeyfile=str(cert_dir / "tls.key"),
    )


def configure_admission(settings: Settings, operator_settings: Any) -> bool:
    """Point kopf's admission settings at the webhook server when enabled.

    ``admission.managed`` stays unset: the Helm chart owns the webhook configuration.

    Args:
        settings: Application settings
        operator_settings: ``kopf.OperatorSettings`` from the startup handler

    Returns:
        True if the webhook server was configured
    """
    if not settings.webhook_enabled:
        return False
    operator_settings.admission.server = build_webhook_server(settings)
    operator_settings.admission.managed = None
    logger.info("webhook_server_configured", port=settings.webhook_port)
    return True
