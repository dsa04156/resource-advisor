"""Continuous route queue observation for training candidate routes.

Every tick lists all Pods, Nodes and Kueue LocalQueues once and stores one fresh
``RouteQueueSnapshot`` per (route, member node) through the same import path as
``POST /route-queue-snapshots``. A snapshot is complete only when the node is Ready
with exactly the configured capacity, every occupant's occupancy is known and no
pending Kueue demand can land on it. A failed read stores fresh incomplete
snapshots without occupants, so an earlier observation is never presented as current.
"""

import argparse
import json
import os
import signal
from pathlib import Path
from threading import Event
from typing import Literal

from pydantic import Field, model_validator

from .backends import BackendError, run
from .contracts import TERMINAL, Contract, Ref, now
from .recommendation import QueueItem, RouteQueueSnapshot, import_evidence, queue_snapshot_from_pods

KINDS = {
    "pods": ("pods", "/api/v1/pods"),
    "nodes": ("nodes", "/api/v1/nodes"),
    "localqueues": ("localqueues.kueue.x-k8s.io", "/apis/kueue.x-k8s.io/v1beta2/localqueues"),
}
MAX_PENDING_ITEMS = 64


class RouteMember(Contract):
    node_ref: Ref
    # Physical devices, or logical slots for a shared route; never mixed.
    capacity: int = Field(ge=1, le=4096)


class ObservedRoute(Contract):
    cluster_ref: Ref
    resource_key: str = Field(min_length=1, max_length=128)
    capacity_unit: Literal["physical_device", "logical_slot"]
    nodes: tuple[RouteMember, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def unique_members(self):
        if len({n.node_ref for n in self.nodes}) != len(self.nodes):
            raise ValueError("duplicate route member")
        return self


class QueueTarget(Contract):
    nodes: tuple[Ref, ...] = Field(min_length=1)
    resource_keys: tuple[str, ...] = Field(min_length=1)


class QueueObserverConfig(Contract):
    project_ref: Ref
    interval_seconds: float = Field(default=15, ge=5, le=60)
    routes: tuple[ObservedRoute, ...] = Field(min_length=1, max_length=64)
    # "namespace/name" -> where its admitted workloads can run. Pending demand in an
    # unlisted LocalQueue may land anywhere, so it fails every route closed.
    queue_targets: dict[str, QueueTarget] = {}


class KubectlReader:
    def __init__(self, kubeconfig=None, *, execute=run):
        self.prefix = ["kubectl"] + (["--kubeconfig", kubeconfig] if kubeconfig else [])
        self.execute = execute

    def list(self, kind):
        argv = self.prefix + ["get", KINDS[kind][0], "--all-namespaces", "-o", "json"]
        return _items(json.loads(self.execute(argv, timeout=20)))


class InClusterReader:
    """Kubernetes API with the Pod's ServiceAccount; the token never leaves this process."""

    SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")

    def list(self, kind):
        import ssl
        import urllib.request

        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        request = urllib.request.Request(
            f"https://{host}:{port}{KINDS[kind][1]}",
            headers={"Authorization": "Bearer " + (self.SA / "token").read_text().strip()},
        )
        context = ssl.create_default_context(cafile=str(self.SA / "ca.crt"))
        try:
            with urllib.request.urlopen(request, timeout=20, context=context) as response:
                return _items(json.load(response))
        except (OSError, ValueError) as exc:
            raise BackendError(type(exc).__name__) from exc


def _items(data):
    if not isinstance(data.get("items"), list) or data.get("metadata", {}).get("continue"):
        raise BackendError("incomplete resource listing")
    return data["items"]


def _allocatable(n, key):
    from .inventory import quantity

    status = n.get("status", {})
    values = {int(quantity(status.get(f, {}).get(key, "0"))) for f in ("capacity", "allocatable")}
    return values.pop() if len(values) == 1 else min(values)


def _node_reasons(n, ref, key, expected):
    if n is None:
        return ["NODE_ABSENT:" + ref]
    reasons = []
    ready = {c.get("type"): c.get("status") for c in n.get("status", {}).get("conditions", [])}
    if ready.get("Ready") != "True":
        reasons.append("NODE_NOT_READY:" + ref)
    if n.get("spec", {}).get("unschedulable"):
        reasons.append("NODE_CORDONED:" + ref)
    observed = _allocatable(n, key)
    if observed != expected:
        reasons.append(f"CAPACITY_MISMATCH:{ref}:{observed}/{expected}")
    return reasons


def _kueue_demand(config, queues, node_ref, key):
    items, reasons = [], []
    for q in queues:
        name = q["metadata"]["namespace"] + "/" + q["metadata"]["name"]
        count = int(q.get("status", {}).get("pendingWorkloads") or 0)
        if not count:
            continue
        target = config.queue_targets.get(name)
        if target is None:
            reasons.append(f"KUEUE_PENDING_UNMAPPED:{name}={count}")
        elif node_ref in target.nodes and key in target.resource_keys:
            reasons.append(f"KUEUE_PENDING_UNKNOWN:{name}={count}")
        else:
            continue
        items += [
            QueueItem(job_id=f"kueue:{name}#{i}") for i in range(min(count, MAX_PENDING_ITEMS))
        ]
    return items, reasons


def _ref(prefix, cluster, node, at):
    return f"{prefix}-{cluster}-{node}-{at:%Y%m%dt%H%M%S%f}"[:96]


def observe(config, reader, *, observed_at, expected_occupancy=None):
    """One snapshot per (route, member node) from a single listing of the cluster."""
    listed, failed = {}, None
    for kind in KINDS:
        try:
            listed[kind] = reader.list(kind)
        except BackendError:
            failed = kind
            break
    evidence = _ref("rqo-tick", "k8s", "all", observed_at)
    result = []
    for route in config.routes:
        for member in route.nodes:
            common = dict(
                ref=_ref("rqo", route.cluster_ref, member.node_ref, observed_at),
                project_ref=config.project_ref,
                cluster_ref=route.cluster_ref,
                route_ref=member.node_ref,
                resource_key=route.resource_key,
                capacity=member.capacity,
                capacity_unit=route.capacity_unit,
                observed_at=observed_at,
                evidence_ref=evidence,
            )
            if failed:
                result.append(
                    RouteQueueSnapshot(
                        **common,
                        backend="kubernetes",
                        complete=False,
                        incomplete_reasons=("OBSERVATION_FAILED:" + failed,),
                        source="kubernetes:pods",
                    )
                )
                continue
            snap = queue_snapshot_from_pods(
                listed["pods"],
                nodes={member.node_ref},
                expected_occupancy=expected_occupancy,
                **common,
            )
            nodes = {n["metadata"]["name"]: n for n in listed["nodes"]}
            reasons = _node_reasons(
                nodes.get(member.node_ref), member.node_ref, route.resource_key, member.capacity
            )
            pending, queued = _kueue_demand(
                config, listed["localqueues"], member.node_ref, route.resource_key
            )
            reasons += queued
            if reasons:
                body = snap.model_dump()
                body["incomplete_reasons"] = snap.incomplete_reasons + tuple(reasons)
                body["complete"] = False
                body["pending"] = snap.pending + tuple(pending)
                snap = RouteQueueSnapshot.model_validate(body)
            result.append(snap)
    return result


def frozen_occupancy(conn, project):
    """Occupancy a non-terminal project job's accepted plan froze; nothing is estimated here."""
    from sqlalchemy import select

    from .store import jobs

    result = {}
    for job_id, state, body in conn.execute(
        select(jobs.c.id, jobs.c.state, jobs.c.body).where(jobs.c.project == project)
    ):
        evidence = (body.get("scheduling_plan") or {}).get("recommendation_evidence") or {}
        seconds = evidence.get("selected_occupancy_seconds")
        if state not in TERMINAL and seconds is not None:
            result[job_id] = float(seconds)
    return result


def run_tick(service, config, reader, *, at=None):
    with service.store.transaction() as conn:
        occupancy = frozen_occupancy(conn, config.project_ref)
    snapshots = observe(config, reader, observed_at=at or now(), expected_occupancy=occupancy)
    for snap in snapshots:
        import_evidence(service, config.project_ref, "route_queue_snapshot", snap)
    return snapshots


def main(argv=None):
    from .database_recovery import database_cycle
    from .service import Service
    from .store import Store

    parser = argparse.ArgumentParser(prog="route-queue-observer")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--database", default=os.getenv("RA_DATABASE_URL", "sqlite:///.state/advisor.sqlite")
    )
    parser.add_argument("--in-cluster", action="store_true")
    parser.add_argument("--kubeconfig")
    parser.add_argument("--heartbeat-path")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    config = QueueObserverConfig.model_validate_json(args.config.read_text())
    reader = InClusterReader() if args.in_cluster else KubectlReader(args.kubeconfig)
    store = Store(args.database)
    service = Service(store)
    stopping = Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    if args.heartbeat_path:
        Path(args.heartbeat_path).unlink(missing_ok=True)
    while not stopping.is_set():
        with database_cycle(store, args.heartbeat_path, once=args.once):
            snapshots = run_tick(service, config, reader)
            print(
                json.dumps(
                    {
                        "observed_at": snapshots[0].observed_at.isoformat(),
                        "routes": {
                            f"{s.cluster_ref}/{s.route_ref}": {
                                "complete": s.complete,
                                "running": len(s.running),
                                "pending": len(s.pending),
                                "reasons": list(s.incomplete_reasons)[:4],
                            }
                            for s in snapshots
                        },
                    }
                ),
                flush=True,
            )
        if args.once:
            return
        stopping.wait(config.interval_seconds)


if __name__ == "__main__":
    main()
