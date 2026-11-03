"""Unit tests for high availability with leader election (F-27)."""

import asyncio
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import kopf
import pytest
import yaml
from kopf._cogs.aiokits import aiotoggles
from kopf._cogs.structs import references
from kopf._core.engines import peering

from carbon_scheduler.config import Settings
from carbon_scheduler.controller.leader import (
    MAX_PRIORITY,
    configure_peering,
    failover_bound_seconds,
    peering_lifetime,
    peering_priority,
    run_kwargs,
)

CHART = Path(__file__).resolve().parents[2] / "charts" / "carbon-scheduler"
PEERINGS = references.Resource(
    group="kopf.dev",
    version="v1",
    plural="clusterkopfpeerings",
    kind="ClusterKopfPeering",
    namespaced=False,
    verbs=["get", "list", "watch", "patch"],
)


def _ha_settings(interval: int = 60) -> Settings:
    return Settings(leader_election=True, reconcile_interval_seconds=interval)


class TestPriorities:
    """kopf pauses every replica sharing the top priority, so replicas must differ."""

    def test_priority_is_stable_per_pod(self) -> None:
        assert peering_priority("carbon-scheduler-7d9f-abcde") == peering_priority(
            "carbon-scheduler-7d9f-abcde"
        )

    def test_replicas_get_distinct_priorities(self) -> None:
        pods = [f"carbon-scheduler-6c5b8d4f7-{i:05d}" for i in range(2000)]
        priorities = {peering_priority(pod) for pod in pods}
        assert len(priorities) == len(pods)

    def test_priority_range(self) -> None:
        for identity in ["a", "b", "carbon-scheduler-0", None]:
            assert 1 <= peering_priority(identity) <= MAX_PRIORITY


class TestKopfWiring:
    """Criterion: kopf peering so only one replica patches Jobs."""

    def test_standalone_without_leader_election(self) -> None:
        assert Settings().leader_election is False
        assert run_kwargs(Settings()) == {"standalone": True}

    def test_run_kwargs_make_peering_mandatory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POD_ID", "carbon-scheduler-abc")
        kwargs = run_kwargs(Settings(leader_election=True, peering_name="cs-carbon"))
        assert kwargs == {
            "standalone": False,
            "peering_name": "cs-carbon",
            "priority": peering_priority("carbon-scheduler-abc"),
        }

    def test_configure_peering(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POD_ID", "carbon-scheduler-abc")
        operator_settings = kopf.OperatorSettings()
        assert configure_peering(_ha_settings(interval=45), operator_settings)
        p = operator_settings.peering
        assert p.mandatory and p.clusterwide and not p.standalone
        assert p.name == "carbon-scheduler"
        assert p.lifetime == 45
        assert p.priority == peering_priority("carbon-scheduler-abc")

    def test_configure_peering_keeps_priority_from_kopf_run(self) -> None:
        operator_settings = kopf.OperatorSettings()
        operator_settings.peering.priority = 1234
        configure_peering(_ha_settings(), operator_settings)
        assert operator_settings.peering.priority == 1234

    def test_configure_peering_disabled(self) -> None:
        operator_settings = kopf.OperatorSettings()
        assert not configure_peering(Settings(), operator_settings)
        assert operator_settings.peering.standalone

    def test_settings_from_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CARBON_LEADER_ELECTION", "true")
        monkeypatch.setenv("CARBON_PEERING_NAME", "my-peering")
        settings = Settings()
        assert settings.leader_election
        assert settings.peering_name == "my-peering"


def _iso(moment: datetime) -> str:
    return moment.isoformat()


async def _decide(
    me: str,
    peers: dict[str, dict[str, Any]],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    paused: bool = True,
) -> bool:
    """Feed one peering event to kopf's own logic; return True if `me` is paused."""
    monkeypatch.setenv("POD_ID", me)
    operator_settings = kopf.OperatorSettings()
    configure_peering(settings, operator_settings)
    monkeypatch.setattr(peering, "touch", AsyncMock())
    monkeypatch.setattr(peering, "clean", AsyncMock())
    conflicts = aiotoggles.Toggle(paused)  # mandatory peering starts paused
    pressure = asyncio.Event()
    pressure.set()  # do not sleep until peers expire
    status = {
        me: {"priority": operator_settings.peering.priority, "lifetime": 60, "lastseen": _iso(datetime.now(UTC))},
        **peers,
    }
    event = {
        "type": "MODIFIED",
        "object": {"metadata": {"name": operator_settings.peering.name}, "status": status},
    }
    await peering.process_peering_event(
        raw_event=event,  # type: ignore[arg-type]
        namespace=None,
        resource=PEERINGS,
        identity=peering.Identity(me),
        settings=operator_settings,
        stream_pressure=pressure,
        conflicts_found=conflicts,
    )
    return conflicts.is_on()


def _leader_and_standby() -> tuple[str, str]:
    a, b = "carbon-scheduler-0", "carbon-scheduler-1"
    return (a, b) if peering_priority(a) > peering_priority(b) else (b, a)


class TestFailover:
    """Criterion: only one replica acts; a standby takes over within 2 intervals."""

    async def test_single_replica_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert not await _decide("carbon-scheduler-0", {}, _ha_settings(), monkeypatch)

    async def test_standby_pauses_while_leader_alive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        leader, standby = _leader_and_standby()
        settings = _ha_settings()
        peers = {
            leader: {
                "priority": peering_priority(leader),
                "lifetime": peering_lifetime(settings),
                "lastseen": _iso(datetime.now(UTC) - timedelta(seconds=30)),
            }
        }
        assert await _decide(standby, peers, settings, monkeypatch, paused=False)

    async def test_leader_keeps_running_next_to_standby(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        leader, standby = _leader_and_standby()
        peers = {
            standby: {
                "priority": peering_priority(standby),
                "lifetime": 60,
                "lastseen": _iso(datetime.now(UTC)),
            }
        }
        assert not await _decide(leader, peers, _ha_settings(), monkeypatch)

    @pytest.mark.parametrize("interval", [10, 60, 300])
    async def test_standby_resumes_one_lifetime_after_crash(
        self, interval: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        leader, standby = _leader_and_standby()
        settings = _ha_settings(interval)
        lifetime = peering_lifetime(settings)
        last_keepalive = datetime.now(UTC) - timedelta(seconds=lifetime + 1)
        peers = {
            leader: {
                "priority": peering_priority(leader),
                "lifetime": lifetime,
                "lastseen": _iso(last_keepalive),
            }
        }
        # The crashed leader is dead after one lifetime: the standby resumes.
        assert not await _decide(standby, peers, settings, monkeypatch)
        # Resume re-lists Jobs and release timers fire at once (no initial delay),
        # so a due Job is released well within two reconcile intervals.
        assert failover_bound_seconds(settings) <= 2 * interval

    async def test_graceful_leader_exit_hands_over_at_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # On drain kopf removes its own record (lifetime 0), so only the standby is left.
        _, standby = _leader_and_standby()
        assert not await _decide(standby, {}, _ha_settings(), monkeypatch)

    def test_release_timer_has_no_initial_delay(self) -> None:
        from carbon_scheduler.controller.handlers import check_release_timer

        registry = kopf.get_default_registry()
        timers = [
            h for h in registry._spawning.get_all_handlers() if h.fn is check_release_timer
        ]
        assert timers and timers[0].initial_delay is None


HELM = os.environ.get("HELM") or shutil.which("helm")


def _render(*sets: str, check: bool = True) -> list[dict[str, Any]]:
    assert HELM is not None
    args = [HELM, "template", "cs", str(CHART), "--kube-version", "1.30.0"]
    for value in sets:
        args += ["--set", value]
    result = subprocess.run(args, check=check, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _kind(docs: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [doc for doc in docs if doc["kind"] == kind]


def _env(docs: list[dict[str, Any]]) -> dict[str, Any]:
    container = _kind(docs, "Deployment")[0]["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value", e.get("valueFrom")) for e in container["env"]}


@pytest.mark.skipif(HELM is None, reason="helm not installed")
class TestHelmChart:
    """Criterion: replicaCount: 2 works, with a PodDisruptionBudget."""

    def test_single_replica_stays_standalone(self) -> None:
        docs = _render()
        assert _env(docs)["CARBON_LEADER_ELECTION"] == "false"
        assert not _kind(docs, "PodDisruptionBudget")
        assert not _kind(docs, "ClusterKopfPeering")

    def test_two_replicas_enable_peering_and_pdb(self) -> None:
        docs = _render("replicaCount=2")
        env = _env(docs)
        assert env["CARBON_LEADER_ELECTION"] == "true"
        assert env["CARBON_PEERING_NAME"] == "cs-carbon-scheduler"
        assert env["POD_ID"] == {"fieldRef": {"fieldPath": "metadata.name"}}
        assert _kind(docs, "Deployment")[0]["spec"]["replicas"] == 2
        assert _kind(docs, "ClusterKopfPeering")[0]["metadata"]["name"] == "cs-carbon-scheduler"
        pdb = _kind(docs, "PodDisruptionBudget")[0]
        assert pdb["spec"]["minAvailable"] == 1
        rules = _kind(docs, "ClusterRole")[0]["rules"]
        assert {
            "apiGroups": ["kopf.dev"],
            "resources": ["clusterkopfpeerings"],
            "verbs": ["get", "list", "watch", "patch"],
        } in rules
        affinity = _kind(docs, "Deployment")[0]["spec"]["template"]["spec"]["affinity"]
        assert "podAntiAffinity" in affinity

    def test_explicit_leader_election_with_one_replica(self) -> None:
        docs = _render("config.leaderElection=true")
        assert _env(docs)["CARBON_LEADER_ELECTION"] == "true"
        assert _kind(docs, "ClusterKopfPeering")
        assert not _kind(docs, "PodDisruptionBudget")

    def test_multiple_replicas_without_election_are_rejected(self) -> None:
        with pytest.raises(RuntimeError, match="leaderElection"):
            _render("replicaCount=2", "config.leaderElection=false", check=False)

    def test_peering_crd_ships_with_chart(self) -> None:
        crd = yaml.safe_load((CHART / "crds" / "clusterkopfpeering.yaml").read_text())
        assert crd["metadata"]["name"] == "clusterkopfpeerings.kopf.dev"
        assert crd["spec"]["scope"] == "Cluster"
