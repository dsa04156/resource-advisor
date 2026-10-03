"""Scoped Slurm observations, independent of Kubernetes and execution qualification."""

import json
import re
import shlex
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from .backends import BackendError, run
from .contracts import Contract, Ref, now
from .inventory import PrometheusReader, ResourceType, TelemetryConfig, signal

SOURCE = "slurm-controller"
IDENTIFIER = r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,95}$"


class SlurmController(Contract):
    transport: Literal["local", "ssh"]
    ssh_target: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_][A-Za-z0-9_@.-]{0,190}$")
    account: str = Field(pattern=IDENTIFIER)
    partition: str = Field(pattern=IDENTIFIER)
    data_parser: Literal["v0.0.42"] = "v0.0.42"

    @model_validator(mode="after")
    def explicit_transport(self):
        if (self.transport == "ssh") != (self.ssh_target is not None):
            raise ValueError("SSH requires a target; local transport must not have one")
        return self


class SlurmInventoryConfig(TelemetryConfig):
    controller: SlurmController | None = None
    node_names: dict[Ref, str] = Field(default_factory=dict)
    gres_types: dict[str, ResourceType] = Field(default_factory=dict)

    @model_validator(mode="after")
    def slurm_scope(self):
        if set(self.node_names) - set(self.node_refs):
            raise ValueError("scheduler node mapping outside authorized pool")
        if self.controller and set(self.node_names) != set(self.node_refs):
            raise ValueError("explicit scheduler name for every authorized node required")
        if len(set(self.node_names.values())) != len(self.node_names) or any(
            not re.fullmatch(IDENTIFIER, name) for name in self.node_names.values()
        ):
            raise ValueError("distinct safe scheduler node names required")
        if any(
            not re.fullmatch(r"gres/[A-Za-z][A-Za-z0-9_.-]*(?::[A-Za-z0-9_.-]+)?", key)
            for key in self.gres_types
        ):
            raise ValueError("explicit GRES count classifications required")
        return self


def integer(value, *, factor=1):
    if type(value) is not int or not 0 <= value <= (2**53 - 1) // factor:
        raise ValueError("missing, negative, noninteger or sentinel allocation")
    return value * factor


def flags(value):
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= 32
        or any(
            not isinstance(v, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", v) for v in value
        )
        or len(set(value)) != len(value)
    ):
        raise ValueError("invalid scheduler flags")
    return value


def tres_values(text):
    if not isinstance(text, str) or len(text) > 16384:
        raise ValueError("missing TRES observation")
    result = {}
    for item in text.split(",") if text else []:
        key, value = item.split("=", 1)
        if key in result or not key:
            raise ValueError("duplicate TRES key")
        result[key] = value
    return result


def resource(capacity, allocatable, allocated, observed, *, factor=1, kind=None):
    kind = kind or ResourceType()
    result = kind.model_dump()
    for name, raw in [
        ("capacity", capacity),
        ("allocatable", allocatable),
        ("requested", allocated),
    ]:
        try:
            result[name] = signal(integer(raw, factor=factor), observed_at=observed, source=SOURCE)
        except ValueError:
            result[name] = signal(source=SOURCE, status="unavailable")
    a, b = result["allocatable"]["value"], result["requested"]["value"]
    result["request_headroom"] = (
        signal(max(0, a - b), observed_at=observed, source=SOURCE)
        if a is not None and b is not None
        else signal(source=SOURCE, status="unavailable")
    )
    return result


class SlurmInventoryCollector(PrometheusReader):
    def __init__(self, config: SlurmInventoryConfig, *, execute=run, client=None):
        super().__init__(config, client=client)
        self.execute = execute

    def read(self, command, collection):
        route = self.config.controller
        if route is None:
            return None, "not_configured"
        if route.transport == "ssh":
            command = [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "ConnectTimeout=5",
                route.ssh_target,
                shlex.join(command),
            ]
        try:
            raw = self.execute(command, timeout=12)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("oversize scheduler response")
            payload = json.loads(raw)
            if (
                payload.get("errors")
                or payload.get("warnings")
                or payload["meta"]["plugin"]["data_parser"] != "data_parser/" + route.data_parser
                or not isinstance(payload[collection], list)
                or len(payload[collection]) > 10000
            ):
                raise ValueError("incomplete or unqualified scheduler response")
            return payload[collection], "ok"
        except (BackendError, ValueError, KeyError, TypeError, AttributeError):
            return None, "unavailable"

    def node_view(self, ref, raw, observed, source_status):
        def unknown():
            return signal(
                source=SOURCE, status=source_status if source_status != "ok" else "unavailable"
            )

        node = {
            "node_ref": ref,
            "hardware": None,
            "resources": {},
            "telemetry": {},
            "scheduler_state": unknown(),
            "accelerator_inventory": unknown(),
            "scheduling_blockers": [],
        }
        if raw is None:
            return node
        try:
            states = flags(raw["state"])
            node["scheduler_state"] = signal(states, observed_at=observed, source=SOURCE)
            node["scheduling_blockers"] = [
                s for s in states if s not in {"IDLE", "MIXED", "ALLOCATED"}
            ]
        except (KeyError, ValueError):
            pass
        node["hardware"] = {
            key: raw.get(source)
            if isinstance(raw.get(source), str) and len(raw[source]) <= 256
            else None
            for key, source in [
                ("architecture", "architecture"),
                ("operatingSystem", "operating_system"),
                ("slurmdVersion", "version"),
            ]
        }
        node["resources"]["cpu"] = resource(
            raw.get("cpus"), raw.get("effective_cpus"), raw.get("alloc_cpus"), observed
        )
        try:
            allocatable_memory = max(
                0, integer(raw["real_memory"]) - integer(raw["specialized_memory"])
            )
        except (KeyError, ValueError):
            allocatable_memory = None
        node["resources"]["memory"] = resource(
            raw.get("real_memory"),
            allocatable_memory,
            raw.get("alloc_memory"),
            observed,
            factor=1024**2,
        )
        try:
            capacities = tres_values(raw.get("tres"))
        except ValueError:
            capacities = {}
        try:
            allocations = tres_values(raw.get("tres_used"))
        except ValueError:
            allocations = {}
        for key, kind in self.config.gres_types.items():
            if key not in capacities:
                continue

            def count(mapping, resource_key):
                v = mapping.get(resource_key)
                return int(v) if isinstance(v, str) and re.fullmatch(r"[0-9]{1,16}", v) else None

            cap, used = count(capacities, key), count(allocations, key)
            allocatable = (
                cap
                if "gres_drained" in raw
                and (raw["gres_drained"] is None or raw["gres_drained"] == "")
                else None
            )
            node["resources"][key] = resource(cap, allocatable, used, observed, kind=kind)
        mapped = [k for k in node["resources"] if k.startswith("gres/")]
        if "gres" in raw:
            status = "ok" if not raw["gres"] or mapped else "unclassified"
            node["accelerator_inventory"] = signal(
                {"reported_gres": bool(raw["gres"]), "mapped_resources": mapped},
                observed_at=observed,
                source=SOURCE,
                status=status,
            )
        return node

    def queue_view(self, jobs, observed, source_status):
        if jobs is None:
            return signal(source=SOURCE, status=source_status)
        route = self.config.controller
        counts, seen = {}, set()
        try:
            for job in jobs:
                if job["account"] != route.account or route.partition not in job["partition"].split(
                    ","
                ):
                    raise ValueError("queue response outside project account/partition")
                identity = integer(job["job_id"])
                if identity in seen:
                    raise ValueError("duplicate scheduler job")
                seen.add(identity)
                base_states = set(flags(job["job_state"])) & {
                    "PENDING",
                    "RUNNING",
                    "SUSPENDED",
                    "COMPLETED",
                    "CANCELLED",
                    "FAILED",
                    "TIMEOUT",
                    "NODE_FAIL",
                    "PREEMPTED",
                    "BOOT_FAIL",
                    "DEADLINE",
                    "OUT_OF_MEMORY",
                }
                if len(base_states) != 1:
                    raise ValueError("unqualified or ambiguous base job state")
                state = next(iter(base_states))
                counts[state] = counts.get(state, 0) + 1
            return signal(
                {
                    "scope": "configured_account_partition_records",
                    "account": route.account,
                    "partition": route.partition,
                    "record_count": len(jobs),
                    "states": counts,
                    "pending_records": counts.get("PENDING", 0),
                    "running_records": counts.get("RUNNING", 0),
                    "array_tasks_expanded": False,
                },
                observed_at=observed,
                source=SOURCE,
            )
        except (KeyError, ValueError, TypeError, AttributeError):
            return signal(source=SOURCE, status="unavailable")

    def collect(self):
        cfg, observed = self.config, now().isoformat()
        parser = cfg.controller.data_parser if cfg.controller else "v0.0.42"
        nodes, node_status = self.read(["scontrol", "--json=" + parser, "show", "nodes"], "nodes")
        node_map = {}
        if nodes is not None:
            try:
                for node in nodes:
                    name = node["name"]
                    if not isinstance(name, str) or name in node_map:
                        raise ValueError("ambiguous scheduler node")
                    node_map[name] = node
            except (KeyError, TypeError, ValueError):
                node_map, node_status = {}, "unavailable"
        route = cfg.controller
        commands = ["squeue", "--json=" + parser]
        if route:
            commands += ["--account=" + route.account, "--partition=" + route.partition]
        jobs, queue_status = self.read(commands, "jobs")
        queue = self.queue_view(jobs, observed, queue_status)
        snapshot = {
            "schema_version": "v1",
            "backend": "slurm",
            "ref": "inv-" + uuid4().hex,
            "cluster_ref": cfg.cluster_ref,
            "collected_at": observed,
            "stale_after_seconds": cfg.stale_after_seconds,
            "status": "ok",
            "sources": {"slurm_nodes": node_status, "slurm_queue": queue["status"]},
            "nodes": [
                self.node_view(ref, node_map.get(cfg.node_names.get(ref)), observed, node_status)
                for ref in cfg.node_refs
            ],
            "queues": [],
            "cluster_queues": [],
            "slurm_queue": queue,
            "execution_qualification": "inventory never grants runtime qualification or admission",
        }
        for binding, sample in zip(cfg.metrics, self.metric_samples(), strict=True):
            target = next(n for n in snapshot["nodes"] if n["node_ref"] == binding.node_ref)
            target["telemetry"][binding.device_ref + ":" + binding.name] = sample
        snapshot["sources"]["prometheus"] = (
            "not_configured"
            if not cfg.metrics
            else "ok"
            if all(s["status"] == "ok" for n in snapshot["nodes"] for s in n["telemetry"].values())
            else "partial"
        )
        if any(v != "ok" for v in snapshot["sources"].values()) or any(
            n["scheduler_state"]["status"] != "ok"
            or any(s["status"] != "ok" for s in n["telemetry"].values())
            for n in snapshot["nodes"]
        ):
            snapshot["status"] = "partial"
        return snapshot
