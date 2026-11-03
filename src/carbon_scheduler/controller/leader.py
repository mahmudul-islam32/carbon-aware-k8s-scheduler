"""High availability via kopf peering (F-27).

With ``CARBON_LEADER_ELECTION=true`` every replica joins the cluster-wide
``ClusterKopfPeering`` object ``CARBON_PEERING_NAME``. kopf lets only the
replica with the highest priority watch Jobs, run timers and patch anything;
the others stay paused until the leader disappears.

* **Distinct priorities.** kopf pauses *all* replicas that share the highest
  priority, so each replica derives its priority from its pod name. Two pods
  practically never collide (31-bit hash).
* **Fast failover.** Each replica advertises a peering lifetime equal to the
  reconcile interval. A leader that stops gracefully (node drain) removes itself
  at once; one that crashes is considered dead one lifetime after its last
  keep-alive. The standby then resumes, re-lists Jobs and its release timers
  fire immediately, so a due Job is released within two reconcile intervals.
* **Fail-safe, not split-brain.** Peering is mandatory once enabled: without
  the CRD or peering object the replica stays paused instead of double-patching.
"""

import hashlib
import os
import random
from typing import Any

import structlog

from carbon_scheduler.config import Settings

logger = structlog.get_logger(__name__)

# kopf priorities are compared as integers; keep them in the positive int32 range.
MAX_PRIORITY = 2**31 - 1
# kopf reads its own identity from this variable; the chart sets it to the pod name.
POD_ID_ENV = "POD_ID"


def pod_identity() -> str | None:
    """Return this replica's pod name, if the downward API provided it."""
    return os.environ.get(POD_ID_ENV) or os.environ.get("HOSTNAME") or None


def peering_priority(identity: str | None) -> int:
    """Derive a stable, practically unique kopf peering priority for a replica.

    Args:
        identity: Pod name (or another unique replica id); ``None`` picks a random value

    Returns:
        Priority in ``[1, MAX_PRIORITY]``
    """
    if not identity:
        return random.randint(1, MAX_PRIORITY)
    digest = hashlib.sha256(identity.encode()).digest()
    return int.from_bytes(digest[:4], "big") % MAX_PRIORITY + 1


def peering_lifetime(settings: Settings) -> int:
    """Seconds a replica's peering record stays valid without a keep-alive.

    Equal to the reconcile interval, so a crashed leader is replaced within one
    interval and its due Jobs are released by the next one.
    """
    return int(settings.reconcile_interval_seconds)


def failover_bound_seconds(settings: Settings) -> int:
    """Worst-case time from a leader crash to a due Job being released.

    One peering lifetime until the standby resumes, plus at most one reconcile
    interval for the re-listing and the first release-timer tick.
    """
    return peering_lifetime(settings) + int(settings.reconcile_interval_seconds)


def run_kwargs(settings: Settings) -> dict[str, Any]:
    """Peering-related keyword arguments for ``kopf.run``.

    Args:
        settings: Application settings

    Returns:
        ``standalone`` plus, with leader election, ``peering_name`` (which makes
        peering mandatory in kopf) and this replica's ``priority``
    """
    if not settings.leader_election:
        return {"standalone": True}
    return {
        "standalone": False,
        "peering_name": settings.peering_name,
        "priority": peering_priority(pod_identity()),
    }


def configure_peering(settings: Settings, operator_settings: Any) -> bool:
    """Apply leader-election settings to kopf's operator settings.

    Called from the startup handler (before kopf starts the peering tasks).

    Args:
        settings: Application settings
        operator_settings: ``kopf.OperatorSettings`` from the startup handler

    Returns:
        True if peering is enabled for this replica
    """
    peering = operator_settings.peering
    if not settings.leader_election:
        peering.standalone = True
        return False

    peering.standalone = False
    peering.mandatory = True
    peering.clusterwide = True
    peering.name = settings.peering_name
    peering.lifetime = peering_lifetime(settings)
    peering.stealth = True  # do not log every keep-alive
    if not peering.priority:
        peering.priority = peering_priority(pod_identity())

    logger.info(
        "leader_election_enabled",
        peering_name=peering.name,
        priority=peering.priority,
        lifetime_seconds=peering.lifetime,
        failover_bound_seconds=failover_bound_seconds(settings),
    )
    return True
