"""Read-only inventory and telemetry; observations never qualify an executable runtime."""

import copy
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

import httpx
from pydantic import Field, model_validator
from sqlalchemy import select

from .backends import BackendError, run
from .contracts import Contract, Ref, now
from .store import entities


class ResourceType(Contract):
    device_class: Literal["gpu", "npu", "unknown"] = "unknown"
    allocation_mode: Literal["physical_device", "virtual_slot", "unknown"] = "unknown"


class MetricBinding(Contract):
    node_ref: Ref
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    device_ref: str = Field(default="node", min_length=1, max_length=96)
    unit: Literal["bytes", "cores", "percent", "watts", "celsius", "count", "hertz", "seconds"]
    query: str = Field(min_length=1, max_length=4096)
    timestamp_query: str = Field(min_length=1, max_length=4096)
    health_query: str | None = Field(default=None, max_length=4096)
    minimum: float | None = None
    maximum: float | None = None

    @model_validator(mode="after")
    def valid_bounds(self):
        if any(v is not None and not math.isfinite(v) for v in (self.minimum, self.maximum)):
            raise ValueError("metric bounds must be finite")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum must not exceed maximum")
        return self


class InventoryConfig(Contract):
    project_ref: Ref
    cluster_ref: Ref
    node_refs: tuple[Ref, ...] = Field(min_length=1, max_length=1000)
    kubeconfig: str | None = None
    queue_namespaces: tuple[Ref, ...] = ()
    cluster_queue_refs: tuple[Ref, ...] = Field(default=(), max_length=64)
    resource_types: dict[str, ResourceType] = Field(default_factory=dict)
    node_resource_types: dict[str, dict[str, ResourceType]] = Field(default_factory=dict)
    stale_after_seconds: int = Field(default=120, ge=5, le=3600)
    prometheus_url: str | None = None
    prometheus_token_env: str | None = None
    metrics: tuple[MetricBinding, ...] = Field(default=(), max_length=200)

    @model_validator(mode="after")
    def bound_sources(self):
        if len(set(self.node_refs)) != len(self.node_refs):
            raise ValueError("duplicate inventory node")
        if len(set(self.cluster_queue_refs)) != len(self.cluster_queue_refs):
            raise ValueError("duplicate authorized ClusterQueue")
        if any(m.node_ref not in self.node_refs for m in self.metrics):
            raise ValueError("metric outside authorized node pool")
        keys = [(m.node_ref, m.device_ref, m.name) for m in self.metrics]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate telemetry binding")
        if self.metrics and not self.prometheus_url:
            raise ValueError("Prometheus endpoint required for bindings")
        if self.prometheus_url:
            url = httpx.URL(self.prometheus_url)
            if url.scheme not in {"http", "https"} or url.userinfo:
                raise ValueError("HTTP(S) endpoint without embedded credentials required")
        return self


def quantity(value):
    """Kubernetes resource quantity in base units (cores, bytes, or typed units)."""
    match = re.fullmatch(
        r"([+]?(?:\d+(?:\.\d*)?|\.\d+))([eE][+-]?\d+|[numkKMGTPE]|[KMGTPE]i)?", str(value)
    )
    if not match:
        raise ValueError("invalid resource quantity")
    number, suffix = match.groups()
    suffix = suffix or ""
    if suffix.endswith("i"):
        factor = Decimal(1024) ** ("KMGTPE".index(suffix[0]) + 1)
    elif suffix.startswith(("e", "E")) and len(suffix) > 1:
        exponent = int(suffix[1:])
        if abs(exponent) > 30:
            raise ValueError("resource exponent out of range")
        factor = Decimal(10) ** exponent
    else:
        exponent = {
            "": 0,
            "n": -9,
            "u": -6,
            "m": -3,
            "k": 3,
            "K": 3,
            "M": 6,
            "G": 9,
            "T": 12,
            "P": 15,
            "E": 18,
        }[suffix]
        factor = Decimal(10) ** exponent
    result = Decimal(number) * factor
    if not result.is_finite() or result > Decimal("1e30"):
        raise ValueError("resource quantity out of range")
    return result


def pod_request(pod, resource):
    spec = pod["spec"]
    if spec.get("resources") or pod.get("status", {}).get("resize"):
        raise ValueError("pod-level resources or in-place resize need qualification")
    if any(c.get("allocatedResources") for c in pod.get("status", {}).get("containerStatuses", [])):
        raise ValueError("in-place allocated resources need qualification")

    def request(container):
        return quantity(container.get("resources", {}).get("requests", {}).get(resource, "0"))

    steady = sum((request(c) for c in spec.get("containers", [])), Decimal(0))
    sidecars = peak = Decimal(0)
    for container in spec.get("initContainers", []):
        amount = request(container)
        if container.get("restartPolicy") == "Always":
            sidecars += amount
            steady += amount
            peak = max(peak, sidecars)
        else:
            peak = max(peak, sidecars + amount)
    return max(steady, peak) + quantity(spec.get("overhead", {}).get(resource, "0"))


def signal(value=None, *, observed_at=None, source, status="ok", unit=None):
    return {
        "value": value,
        "observed_at": observed_at,
        "source": source,
        "status": status,
        "unit": unit,
    }


def fresh_view(snapshot, *, at=None):
    """Re-evaluate freshness on every read, including when the collector stopped."""
    snapshot = copy.deepcopy(snapshot)
    at = at or now()
    age = (at - datetime.fromisoformat(snapshot["collected_at"])).total_seconds()
    expired = age < -5 or age > snapshot["stale_after_seconds"]
    snapshot["status"] = "stale" if expired else snapshot["status"]

    def visit(value):
        if isinstance(value, dict):
            if "source" in value and "value" in value and "observed_at" in value:
                state = value["status"]
                if state == "ok":
                    try:
                        stamp = datetime.fromisoformat(value["observed_at"])
                        seconds = (at - stamp).total_seconds()
                        if stamp.tzinfo is None or seconds < -5:
                            state = "invalid_time"
                        elif expired or seconds > snapshot["stale_after_seconds"]:
                            state = "stale"
                    except (ValueError, TypeError):
                        state = "invalid_time"
                value["status"] = state
                if state != "ok":
                    value["value"] = None
                    if not expired:
                        snapshot["status"] = "partial"
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(snapshot)
    return snapshot


class InventoryCollector:
    def __init__(self, config: InventoryConfig, *, execute=run, client=None):
        self.config, self.execute, self.client = config, execute, client
        self.prefix = ["kubectl"] + (
            ["--kubeconfig", config.kubeconfig] if config.kubeconfig else []
        )

    def read(self, *arguments):
        import json

        return json.loads(self.execute(self.prefix + list(arguments), timeout=15))

    def metric(self, binding, client):
        source = "prometheus"

        def scalar(query):
            response = client.get("/api/v1/query", params={"query": query, "timeout": "8s"})
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") != "success" or payload.get("warnings"):
                raise ValueError("incomplete Prometheus response")
            data = payload["data"]
            if data["resultType"] != "vector" or len(data["result"]) != 1:
                raise ValueError("one unambiguous sample required")
            value = float(data["result"][0]["value"][1])
            if not math.isfinite(value):
                raise ValueError("nonfinite sample")
            return value

        try:
            if binding.health_query and scalar(binding.health_query) != 1:
                return signal(source=source, status="collector_unhealthy", unit=binding.unit)
            # Evaluation timestamps are NOT source sample timestamps. The
            # operator supplies timestamp(raw_metric) or a conservative minimum.
            value = scalar(binding.query)
            stamp = datetime.fromtimestamp(scalar(binding.timestamp_query), UTC).isoformat()
            if (
                (binding.unit == "percent" and not 0 <= value <= 100)
                or (binding.unit != "celsius" and value < 0)
                or (binding.minimum is not None and value < binding.minimum)
                or (binding.maximum is not None and value > binding.maximum)
            ):
                raise ValueError("sample outside unit range")
            return signal(value, observed_at=stamp, source=source, unit=binding.unit)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, OverflowError):
            return signal(source=source, status="unavailable", unit=binding.unit)

    def collect(self):
        from .queue_inventory import queue_view, workload_view

        cfg = self.config
        observed = now().isoformat()
        snapshot = {
            "schema_version": "v1",
            "ref": "inv-" + uuid4().hex,
            "cluster_ref": cfg.cluster_ref,
            "collected_at": observed,
            "stale_after_seconds": cfg.stale_after_seconds,
            "status": "ok",
            "sources": {},
            "nodes": [],
            "queues": [],
            "cluster_queues": [],
            "execution_qualification": "inventory never grants runtime qualification",
        }

        def get(name, *arguments):
            try:
                data = self.read(*arguments)
                if not isinstance(data.get("items"), list) or data.get("metadata", {}).get(
                    "continue"
                ):
                    raise ValueError("incomplete resource listing")
                snapshot["sources"][name] = "ok"
                return data["items"]
            except (BackendError, ValueError, KeyError, TypeError):
                snapshot["sources"][name] = "unavailable"
                snapshot["status"] = "partial"
                return None

        raw_nodes = get("nodes", "get", "nodes", "-o", "json")
        pods = get("pods", "get", "pods", "--all-namespaces", "-o", "json")
        metrics = get("metrics_api", "get", "--raw", "/apis/metrics.k8s.io/v1beta1/nodes")
        node_map = {n["metadata"]["name"]: n for n in raw_nodes or []}
        metric_map = {n["metadata"]["name"]: n for n in metrics or []}
        for ref in cfg.node_refs:
            n = node_map.get(ref)
            result = {
                "node_ref": ref,
                "hardware": None,
                "resources": {},
                "telemetry": {},
                "ready": signal(source="kubernetes", status="unavailable"),
                "scheduling_blockers": [],
            }
            snapshot["nodes"].append(result)
            if n is None:
                snapshot["status"] = "partial"
            if n is not None:
                info = n.get("status", {}).get("nodeInfo", {})
                result["hardware"] = {
                    k: info.get(k)
                    for k in [
                        "architecture",
                        "operatingSystem",
                        "osImage",
                        "kernelVersion",
                        "kubeletVersion",
                        "containerRuntimeVersion",
                    ]
                }
                conditions = {c["type"]: c["status"] for c in n["status"].get("conditions", [])}
                ready = conditions.get("Ready") == "True"
                result["ready"] = signal(ready, observed_at=observed, source="kubernetes")
                result["scheduling_blockers"] = [
                    k for k, v in conditions.items() if k.endswith("Pressure") and v != "False"
                ]
                if not ready:
                    result["scheduling_blockers"].append("NotReady")
                if n["spec"].get("unschedulable"):
                    result["scheduling_blockers"].append("Cordoned")
                result["taints"] = [
                    {k: t.get(k) for k in ["key", "effect"]} for t in n["spec"].get("taints", [])
                ]
                allocated = n["status"].get("allocatable", {})
                capacity = n["status"].get("capacity", {})
                current = [
                    p
                    for p in pods or []
                    if p.get("spec", {}).get("nodeName") == ref
                    and p.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
                ]
                for key in sorted(set(allocated) | set(capacity)):
                    if key not in {"cpu", "memory", "ephemeral-storage", "pods"} and "/" not in key:
                        continue
                    kind = cfg.node_resource_types.get(ref, {}).get(
                        key, cfg.resource_types.get(key)
                    )
                    resource = {
                        "device_class": kind.device_class if kind else "unknown",
                        "allocation_mode": kind.allocation_mode if kind else "unknown",
                    }
                    result["resources"][key] = resource
                    for field, values in [("capacity", capacity), ("allocatable", allocated)]:
                        try:
                            amount = float(quantity(values[key]))
                            resource[field] = signal(
                                amount, observed_at=observed, source="kubernetes"
                            )
                        except (ValueError, KeyError):
                            resource[field] = signal(source="kubernetes", status="unavailable")
                    try:
                        if pods is None:
                            raise ValueError("pod list unavailable")
                        requested = (
                            Decimal(len(current))
                            if key == "pods"
                            else sum((pod_request(p, key) for p in current), Decimal(0))
                        )
                        resource["requested"] = signal(
                            float(requested), observed_at=observed, source="kubernetes"
                        )
                        remaining = max(Decimal(0), quantity(allocated[key]) - requested)
                        resource["request_headroom"] = signal(
                            float(remaining), observed_at=observed, source="kubernetes"
                        )
                    except (ValueError, KeyError):
                        for field in ["requested", "request_headroom"]:
                            resource[field] = signal(source="kubernetes", status="unavailable")
            m = metric_map.get(ref)
            for name, key, unit in [
                ("cpu_usage_cores", "cpu", "cores"),
                ("memory_working_set_bytes", "memory", "bytes"),
            ]:
                try:
                    value = float(quantity(m["usage"][key]))
                    result["telemetry"][name] = signal(
                        value, observed_at=m["timestamp"], source="metrics_api", unit=unit
                    )
                except (ValueError, KeyError, TypeError):
                    result["telemetry"][name] = signal(
                        source="metrics_api", status="unavailable", unit=unit
                    )
        for namespace in cfg.queue_namespaces:
            workloads = get(
                "kueue:" + namespace,
                "get",
                "workloads.kueue.x-k8s.io",
                "-n",
                namespace,
                "-o",
                "json",
            )
            active = []
            for w in workloads or []:
                view = workload_view(w)
                if view is not None:
                    active.append(view)
            local = get(
                "localqueues:" + namespace,
                "get",
                "localqueues.kueue.x-k8s.io",
                "-n",
                namespace,
                "-o",
                "json",
            )
            snapshot["queues"].append(
                {
                    "namespace": namespace,
                    "local_queues": signal(
                        [queue_view(q, cfg.resource_types) for q in local]
                        if local is not None
                        else None,
                        observed_at=observed,
                        source="kueue",
                        status="ok" if local is not None else "unavailable",
                    ),
                    "workloads": signal(
                        active if workloads is not None else None,
                        observed_at=observed,
                        source="kueue",
                        status="ok" if workloads is not None else "unavailable",
                    ),
                }
            )
        for ref in cfg.cluster_queue_refs:
            source = "clusterqueue:" + ref
            try:
                raw = self.read("get", "clusterqueues.kueue.x-k8s.io", ref, "-o", "json")
                if raw["metadata"]["name"] != ref:
                    raise ValueError("ClusterQueue response outside authorized scope")
                view = queue_view(raw, cfg.resource_types, cluster=True)
                snapshot["sources"][source] = "ok"
                observed_queue = signal(view, observed_at=observed, source="kueue")
            except (BackendError, ValueError, KeyError, TypeError):
                snapshot["sources"][source] = "unavailable"
                snapshot["status"] = "partial"
                observed_queue = signal(observed_at=observed, source="kueue", status="unavailable")
            snapshot["cluster_queues"].append({"ref": ref, "observation": observed_queue})
        if cfg.metrics:
            token = os.environ.get(cfg.prometheus_token_env) if cfg.prometheus_token_env else None
            if cfg.prometheus_token_env and not token:
                samples = [
                    signal(source="prometheus", status="unavailable", unit=b.unit)
                    for b in cfg.metrics
                ]
            else:
                headers = {"Authorization": "Bearer " + token} if token else {}
                client = self.client or httpx.Client(
                    base_url=cfg.prometheus_url, headers=headers, timeout=10
                )
                try:
                    with ThreadPoolExecutor(max_workers=4) as pool:
                        samples = list(pool.map(lambda b: self.metric(b, client), cfg.metrics))
                finally:
                    if self.client is None:
                        client.close()
            for binding, sample in zip(cfg.metrics, samples, strict=True):
                target = next(n for n in snapshot["nodes"] if n["node_ref"] == binding.node_ref)
                target["telemetry"][binding.device_ref + ":" + binding.name] = sample
                if sample["status"] != "ok":
                    snapshot["status"] = "partial"
        return snapshot


def save_inventory(store, project, snapshot):
    with store.transaction() as conn:
        store.put(conn, "inventory", snapshot["ref"], project, snapshot)


def latest_inventory(store, project, cluster):
    with store.transaction() as conn:
        row = (
            conn.execute(
                select(entities)
                .where(
                    entities.c.kind == "inventory",
                    entities.c.project == project,
                    entities.c.body["cluster_ref"].as_string() == cluster,
                )
                .order_by(entities.c.created_at.desc(), entities.c.ref.desc())
                .limit(1)
            )
            .mappings()
            .first()
        )
        return fresh_view(row["body"]) if row else None
