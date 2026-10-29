"""Separate adapters. Kubernetes admission and Slurm scheduling stay independent."""

import json
import math
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .accounting import number
from .contracts import State, now


class BackendError(RuntimeError):
    pass


class SubmissionUnknown(BackendError):
    """Submission might have reached the scheduler. Never automatically resubmit."""


@dataclass
class Observation:
    state: State
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    allocation: dict | None = None
    submitted_at: str | None = None
    execution_started_at: str | None = None
    backend_uid: str | None = None
    termination: dict | None = None
    scheduler_reason: str | None = None
    # Observed native identity/placement and admission, not planned capability.
    placement: dict | None = None
    admission: dict | None = None


def memory_mib(value, *, slurm=False):
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGT]i?|[kMGT])?", str(value))
    if not match:
        return None
    amount, unit = match.groups()
    unit = unit or ""
    if slurm:
        factors = {"": 1, "K": 1 / 1024, "M": 1, "G": 1024, "T": 1024**2}
    else:
        factors = {
            "": 1 / 1024**2,
            "Ki": 1 / 1024,
            "Mi": 1,
            "Gi": 1024,
            "Ti": 1024**2,
            "k": 1000 / 1024**2,
            "M": 1000**2 / 1024**2,
            "G": 1000**3 / 1024**2,
            "T": 1000**4 / 1024**2,
        }
    return float(amount) * factors[unit] if unit in factors else None


def slurm_allocation(text, resource_key):
    if not text or "..." in text:
        return None
    tres = dict(part.split("=", 1) for part in text.split(",") if "=" in part)
    generic = "gres/" + (resource_key or "gpu").split(":")[0]
    count = tres.get(generic, tres.get("gres/" + (resource_key or "gpu")))
    return {
        "source": "slurm:sacct AllocTRES",
        "accelerator_count": number(count),
        "cpu": number(tres.get("cpu")),
        "memory_mib": memory_mib(tres.get("mem"), slurm=True),
    }


class SchedulerBackend(Protocol):
    def validate(self, job: dict) -> None: ...
    def submit(self, job: dict) -> str: ...
    def reconcile(self, job: dict) -> str | None: ...
    def status(self, job: dict) -> Observation: ...
    def cancel(self, job: dict) -> None: ...
    def result(self, job: dict) -> dict: ...


def run(argv, *, stdin=None, timeout=30):
    try:
        proc = subprocess.run(
            argv, input=stdin, text=True, capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackendError(type(exc).__name__) from exc
    if proc.returncode:
        # Backend output may contain site paths or credentials: keep it out of public API.
        raise BackendError(f"{argv[0]} exited with code {proc.returncode}")
    return proc.stdout


def result_from_log(log: str) -> dict:
    marker = "RESOURCE_ADVISOR_RESULT "
    lines = [line[len(marker) :] for line in log.splitlines() if line.startswith(marker)]
    if len(lines) != 1 or len(lines[0]) > 65536:
        raise BackendError("one bounded result envelope is required")
    return json.loads(lines[0])


PROGRESS_FIELDS = {
    "epoch": int,
    "total_epochs": int,
    "optimizer_steps": int,
    "loss": float,
    "elapsed_seconds": float,
    "observed_at": str,
}


def progress_from_log(log, job, source):
    """Bounded tail plus validated progress records of this attempt only."""
    records = []
    for line in log.splitlines():
        if not line.startswith("RA_PROGRESS ") or len(line) > 2048:
            continue
        try:
            value = json.loads(line[len("RA_PROGRESS ") :])
            record = {key: kind(value[key]) for key, kind in PROGRESS_FIELDS.items()}
        except (ValueError, KeyError, TypeError):
            continue
        if (
            value.get("schema") == "ra-progress-v1"
            and value.get("attempt_id") == job["body"]["attempt_id"]
            and value.get("job_id") == job["id"]
            and all(math.isfinite(record[k]) for k in ("loss", "elapsed_seconds"))
            and len(record["observed_at"]) <= 64
        ):
            records.append(record)
    tail = [
        "[result envelope]" if line.startswith("RESOURCE_ADVISOR_RESULT ") else line[:300]
        for line in log.splitlines()[-40:]
    ]
    return {
        "source": source,
        "attempt_id": job["body"]["attempt_id"],
        "observed_at": now().isoformat(),
        "latest": records[-1] if records else None,
        "records": records[-20:],
        "log_tail": tail,
    }


DOWNWARD_IDENTITY = (
    ("RA_NODE_NAME", "spec.nodeName"),
    ("RA_POD_NAME", "metadata.name"),
    ("RA_POD_UID", "metadata.uid"),
)


def identity_environment(job):
    b = job["body"]
    values = {
        "RA_JOB_ID": job["id"],
        "RA_ATTEMPT_ID": b["attempt_id"],
        "RA_EPOCH": str(b["epoch"]),
        "RA_WORKLOAD_SIGNATURE": b["workload_signature"],
        "RA_CONTEXT_SIGNATURE": b["context_signature"],
        "RA_CONTEXT_JSON": json.dumps(b["candidate"]["context"], separators=(",", ":")),
        "RA_WORK_UNITS": str(b["spec"]["identity"]["work_units"]),
        "RA_INPUT_SHAPE": json.dumps(b["spec"]["identity"]["input_shape"]),
        "RA_PRECISION": b["spec"]["identity"]["precision"],
        "RA_SEED": str(b["spec"]["identity"]["seed"]),
        "RA_EXECUTION_MODE": b["request"]["mode"],
        "RA_ARTIFACT_PREFIX": b.get("artifact_prefix", ""),
    }
    if b.get("sampling_binding"):
        values["RA_SAMPLING_PLAN_JSON"] = json.dumps(
            b["sampling_binding"]["plan"], separators=(",", ":")
        )
    if b["variant"].get("thermal_policy"):
        values["RA_THERMAL_POLICY_JSON"] = json.dumps(
            b["variant"]["thermal_policy"], separators=(",", ":")
        )
    if b["variant"].get("load_context_policy"):
        values["RA_LOAD_CONTEXT_POLICY"] = b["variant"]["load_context_policy"]
    staging = (b.get("scheduling_plan") or {}).get("staging_bindings")
    if staging:
        # The accepted plan's cache destination per node; the workload must use it.
        values["RA_STAGING_BINDINGS_JSON"] = json.dumps(staging, separators=(",", ":"))
    return values


def pool_target(job):
    """Opt-in pool target recorded in the accepted plan; None keeps node pinning."""
    target = (job["body"].get("scheduling_plan") or {}).get("target")
    if target is None:
        return None
    if target.get("mode") != "pool" or not target.get("members"):
        raise BackendError("invalid pool target")
    return target


def priority_mapping(mapping):
    mapping = dict(mapping or {})
    for grade, name in mapping.items():
        if (
            grade not in {"normal", "high"}
            or not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", name)
        ):
            raise ValueError("priority mappings need normal/high grades and safe policy names")
    return mapping


def policy_priority(job, mapping, default=None):
    # Names and numeric weights belong to the administrator's project route,
    # not the workload. Older immutable contracts imply normal priority.
    grade = job["body"]["spec"]["execution"].get("priority", "normal")
    if grade in mapping:
        return mapping[grade]
    if grade == "normal":
        return default
    raise BackendError("requested priority is not enabled for this project/backend")


RDZV_PORT = 29400
INDEX_ANNOTATION = "batch.kubernetes.io/job-completion-index"
ATTEMPT_LABEL = "resource-advisor/attempt-id"


def distributed(job):
    """The signed DistributedTraining of this attempt, or None for single-Pod jobs."""
    return job["body"]["candidate"]["context"].get("distributed")


def torchrun_command(command, d, attempt):
    """torchrun (``python -m torch.distributed.run``) around the qualified command.

    Documented c10d rendezvous hosted by completion index 0 (stable DNS through
    the attempt's headless Service); no restarts, so one rank failure ends all.
    """
    if len(command) < 2 or command[0] not in {"python", "python3"}:
        raise BackendError("DISTRIBUTED_COMMAND_UNSUPPORTED")
    if command[1] == "-m" and len(command) < 3:
        raise BackendError("DISTRIBUTED_COMMAND_UNSUPPORTED")
    target = list(command[1:])  # script path or ``-m module``, then its arguments
    return [
        command[0],
        "-m",
        "torch.distributed.run",
        f"--nnodes={len(d['nodes'])}",
        f"--nproc-per-node={d['nproc_per_node']}",
        "--rdzv-backend=c10d",
        f"--rdzv-id={attempt}",
        f"--rdzv-endpoint={attempt}-0.{attempt}:{RDZV_PORT}",
        "--max-restarts=0",
        *target,
    ]


class KubernetesBackend:
    def __init__(
        self,
        *,
        namespace,
        local_queue,
        node_selector,
        kubeconfig=None,
        context=None,
        runtime_bundles=None,
        training_sources=None,
        priority_classes=None,
        retain_termination_evidence=False,
        runtime_class_name=None,
        host_network_variants=None,
        observe_admission=False,
        observe_progress=False,
        execute=run,
    ):
        if not namespace or not local_queue or not node_selector:
            raise ValueError("explicit namespace, LocalQueue and allowed node pool are required")
        self.namespace, self.local_queue, self.node_selector = namespace, local_queue, node_selector
        self.execute = execute
        self.runtime_bundles = runtime_bundles or {}
        self.training_sources = training_sources or {}
        if not isinstance(self.training_sources, dict) or any(
            not isinstance(route, dict) or not isinstance(route.get("source"), dict)
            for route in self.training_sources.values()
        ):
            raise ValueError("training sources require mappings with a source mapping")
        self.priority_classes = priority_mapping(priority_classes)
        if not isinstance(retain_termination_evidence, bool):
            raise ValueError("termination retention must be an explicit boolean")
        self.retain_termination_evidence = retain_termination_evidence
        if runtime_class_name is not None and not re.fullmatch(
            r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", runtime_class_name
        ):
            raise ValueError("invalid Kubernetes runtime class")
        self.runtime_class_name = runtime_class_name
        if not isinstance(observe_admission, bool):
            raise ValueError("Kueue admission observation must be an explicit boolean")
        # Opt-in: reading Workloads needs a namespaced get/list grant in the worker Role.
        self.observe_admission = observe_admission
        if not isinstance(observe_progress, bool):
            raise ValueError("progress observation must be an explicit boolean")
        self.observe_progress = observe_progress
        self.host_network_variants = dict(host_network_variants or {})
        if any(
            not isinstance(ref, str)
            or not isinstance(digest, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
            for ref, digest in self.host_network_variants.items()
        ):
            raise ValueError("host network requires explicit variant/environment digest bindings")
        self.prefix = ["kubectl"]
        if kubeconfig:
            self.prefix += ["--kubeconfig", kubeconfig]
        if context:
            self.prefix += ["--context", context]
        self.prefix += ["--namespace", namespace]

    def validate(self, job):
        self.manifest(job)

    def manifest(self, job):
        b = job["body"]
        priority_class = policy_priority(job, self.priority_classes)
        from .scheduling import verify_adapter_plan

        try:
            verify_adapter_plan(
                job,
                {
                    "namespace": self.namespace,
                    "local_queue": self.local_queue,
                    "runtime_class_name": self.runtime_class_name,
                    "priority_class": priority_class,
                },
            )
        except ValueError as exc:
            raise BackendError(str(exc)) from exc
        r = b["candidate"]["context"]["resources"]
        resources = {"cpu": str(r["host_cpu"]), "memory": f"{r['host_memory_mib']}Mi"}
        key = b["capability"]["resource_key"]
        if r["accelerator_count"]:
            if not key:
                raise BackendError("accelerator resource key missing")
            resources[key] = str(r["accelerator_count"])
        selectors = dict(self.node_selector)
        if selectors.get("kubernetes.io/arch", b["variant"]["arch"]) != b["variant"]["arch"]:
            raise BackendError("configured pool architecture mismatch")
        selectors["kubernetes.io/arch"] = b["variant"]["arch"]
        allowed_host = selectors.get("kubernetes.io/hostname")
        target = pool_target(job)
        affinity = None
        if target:
            # kube-scheduler chooses among verified members only; never drop the
            # hostname restriction without replacing it by the member allowlist.
            members = sorted(m["node_ref"] for m in target["members"])
            if allowed_host and members != [allowed_host]:
                raise BackendError(
                    "pool placement conflicts with the configured hostname allowlist"
                )
            for key, value in target.get("kubernetes_node_selector", {}).items():
                if selectors.get(key, value) != value:
                    raise BackendError("pool selector conflicts with the configured node pool")
                selectors[key] = value
            affinity = {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": "kubernetes.io/hostname",
                                        "operator": "In",
                                        "values": members,
                                    }
                                ]
                            }
                        ]
                    }
                }
            }
        else:
            if allowed_host and allowed_host != b["capability"]["node_ref"]:
                raise BackendError("candidate is outside the configured node allowlist")
            # Select the verified node through scheduling, never spec.nodeName.
            selectors["kubernetes.io/hostname"] = b["capability"]["node_ref"]
        manifest = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": b["attempt_id"],
                "namespace": self.namespace,
                "labels": {
                    "app.kubernetes.io/managed-by": "resource-advisor",
                    "kueue.x-k8s.io/queue-name": self.local_queue,
                    **({"kueue.x-k8s.io/priority-class": priority_class} if priority_class else {}),
                },
                "annotations": {
                    "resource-advisor/job-id": job["id"],
                    "resource-advisor/context-signature": b["context_signature"],
                },
            },
            "spec": {
                "suspend": True,
                "backoffLimit": 0,
                "activeDeadlineSeconds": b.get("execution_limits", b["spec"]["execution"])[
                    "max_run_seconds"
                ],
                "template": {
                    "metadata": {"annotations": {"sidecar.istio.io/inject": "false"}},
                    "spec": {
                        "restartPolicy": "Never",
                        "automountServiceAccountToken": False,
                        "terminationGracePeriodSeconds": 5,
                        "nodeSelector": selectors,
                        "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                        "containers": [
                            {
                                "name": "workload",
                                "image": b["variant"]["image"],
                                "command": b.get("effective_command", b["variant"]["command"]),
                                "env": [
                                    {"name": k, "value": v}
                                    for k, v in identity_environment(job).items()
                                ]
                                # Scheduler-owned identity (Downward API). A collector
                                # trusts it only after matching the observed Pod.
                                + [
                                    {"name": name, "valueFrom": {"fieldRef": {"fieldPath": path}}}
                                    for name, path in DOWNWARD_IDENTITY
                                ],
                                "resources": {"requests": resources, "limits": dict(resources)},
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                            }
                        ],
                    },
                },
            },
        }
        host_transport = (
            b["candidate"]["context"]["runtime_versions"].get("transport_mode") == "host-persistent"
        )
        approved_environment = self.host_network_variants.get(b["variant"]["ref"])
        if self.host_network_variants and not host_transport:
            raise BackendError(
                "dedicated host network route requires a qualified transport variant"
            )
        if host_transport:
            if approved_environment != b["variant"]["environment_digest"]:
                raise BackendError(
                    "host network transport requires an operator-qualified variant binding"
                )
            manifest["spec"]["template"]["spec"].update(
                hostNetwork=True, dnsPolicy="ClusterFirstWithHostNet"
            )
        if affinity:
            manifest["spec"]["template"]["spec"]["affinity"] = affinity
        if self.runtime_class_name:
            manifest["spec"]["template"]["spec"]["runtimeClassName"] = self.runtime_class_name
        bundle_ref = b["variant"].get("kubernetes_runtime_bundle_ref")
        bundle = self.runtime_bundles.get(bundle_ref or b["variant"]["ref"])
        if bundle_ref and not bundle:
            raise BackendError("required qualified Kubernetes runtime bundle is unavailable")
        if self.retain_termination_evidence:
            from .kubernetes_retention import FINALIZER

            manifest["spec"]["template"]["metadata"]["finalizers"] = [FINALIZER]
        if bundle:
            if bundle["environment_digest"] != b["variant"]["environment_digest"]:
                raise BackendError("configured runtime bundle does not match qualified environment")
            pod = manifest["spec"]["template"]["spec"]
            pod["volumes"] = [
                {
                    "name": "qualified-runtime",
                    "persistentVolumeClaim": {"claimName": bundle["pvc"], "readOnly": True},
                },
                {"name": "qualified-source", "configMap": {"name": bundle["source_config_map"]}},
            ]
            container = pod["containers"][0]
            container["volumeMounts"] = [
                {
                    "name": "qualified-runtime",
                    "mountPath": "/opt/qualified-runtime",
                    "readOnly": True,
                },
                {
                    "name": "qualified-source",
                    "mountPath": "/opt/resource-advisor/resource_advisor",
                    "readOnly": True,
                },
            ]
            container["env"].append(
                {"name": "PYTHONPATH", "value": "/opt/resource-advisor:/opt/qualified-runtime/site"}
            )
            container["env"].append({"name": "PYTHONDONTWRITEBYTECODE", "value": "1"})
        if b["spec"]["identity"]["task_type"] == "training":
            from .training import isolate_manifest

            if not b.get("training_isolation"):
                raise BackendError("training isolation binding required")
            try:
                isolate_manifest(manifest, job, self.training_sources)
            except ValueError as exc:
                raise BackendError("invalid training source binding") from exc
        if distributed(job):
            self.distribute(manifest, job, distributed(job))
        return manifest

    def distribute(self, manifest, job, d):
        """One Indexed Job: a Pod per allocated node, per-Pod GPU request, Kueue-gated.

        The node set is enforced by required node affinity plus hostname
        anti-affinity between this attempt's Pods; which index lands on which
        node is the scheduler's choice and is verified from observation.
        """
        b, attempt = job["body"], job["body"]["attempt_id"]
        nodes = [n["node_ref"] for n in d["nodes"]]
        if pool_target(job):
            raise BackendError("DISTRIBUTED_POOL_TARGET_UNSUPPORTED")
        if self.retain_termination_evidence:
            raise BackendError("DISTRIBUTED_TERMINATION_RETENTION_UNSUPPORTED")
        pod = manifest["spec"]["template"]["spec"]
        if pod.get("hostNetwork"):
            raise BackendError("DISTRIBUTED_HOST_NETWORK_UNSUPPORTED")
        allowed_host = self.node_selector.get("kubernetes.io/hostname")
        if allowed_host and nodes != [allowed_host]:
            raise BackendError(
                "distributed node set conflicts with the configured hostname allowlist"
            )
        if b["capability"]["node_ref"] not in nodes:
            raise BackendError("candidate capability node is outside the distributed node set")
        if not re.fullmatch(r"[a-z]([-a-z0-9]{0,61}[a-z0-9])?", attempt):
            raise BackendError("attempt id is not a valid rendezvous Service name")
        pod["nodeSelector"].pop("kubernetes.io/hostname", None)
        affinity = {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {
                            "matchExpressions": [
                                {"key": "kubernetes.io/hostname", "operator": "In", "values": nodes}
                            ]
                        }
                    ]
                }
            }
        }
        if len(nodes) > 1:
            affinity["podAntiAffinity"] = {
                "requiredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "labelSelector": {"matchLabels": {ATTEMPT_LABEL: attempt}},
                        "topologyKey": "kubernetes.io/hostname",
                    }
                ]
            }
        pod["affinity"] = affinity
        pod["subdomain"] = attempt
        manifest["spec"]["template"]["metadata"].setdefault("labels", {})[ATTEMPT_LABEL] = attempt
        manifest["spec"].update(
            completionMode="Indexed", completions=len(nodes), parallelism=len(nodes), backoffLimit=0
        )
        main = pod["containers"][0]
        main["command"] = torchrun_command(main["command"], d, attempt)
        main["env"] += [
            {"name": "RA_DISTRIBUTED_JSON", "value": json.dumps(d, sort_keys=True)},
            {
                "name": "RA_NODE_INDEX",
                "valueFrom": {
                    "fieldRef": {"fieldPath": f"metadata.annotations['{INDEX_ANNOTATION}']"}
                },
            },
        ]
        main["ports"] = [{"name": "rdzv", "containerPort": RDZV_PORT}]

    def service_manifest(self, job, obj):
        """Headless rendezvous Service owned by this exact Job UID (GC with the Job)."""
        attempt, uid = job["body"]["attempt_id"], obj["metadata"]["uid"]
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": attempt,
                "namespace": self.namespace,
                "labels": {"app.kubernetes.io/managed-by": "resource-advisor"},
                "annotations": {"resource-advisor/job-id": job["id"]},
                "ownerReferences": [
                    {
                        "apiVersion": "batch/v1",
                        "kind": "Job",
                        "name": obj["metadata"]["name"],
                        "uid": uid,
                        "controller": True,
                        "blockOwnerDeletion": True,
                    }
                ],
            },
            "spec": {
                "clusterIP": "None",
                # Rendezvous happens before any rank is Ready.
                "publishNotReadyAddresses": True,
                "selector": {ATTEMPT_LABEL: attempt, "batch.kubernetes.io/controller-uid": uid},
                "ports": [{"name": "rdzv", "port": RDZV_PORT, "targetPort": RDZV_PORT}],
            },
        }

    def ensure_service(self, job, obj):
        """Create the owned Service once the Job UID is known; never adopt others."""
        attempt, uid = job["body"]["attempt_id"], obj["metadata"]["uid"]
        raw = self.execute(
            self.prefix + ["get", "service", attempt, "--ignore-not-found", "-o", "json"]
        )
        if raw.strip():
            existing = json.loads(raw)
            if (
                not any(
                    o.get("kind") == "Job" and o.get("uid") == uid
                    for o in existing["metadata"].get("ownerReferences", [])
                )
                or existing["metadata"].get("annotations", {}).get("resource-advisor/job-id")
                != job["id"]
            ):
                raise BackendError("DISTRIBUTED_SERVICE_CONFLICT")
            return
        self.execute(
            self.prefix + ["create", "-f", "-", "-o", "json"],
            stdin=json.dumps(self.service_manifest(job, obj)),
        )

    def submit(self, job):
        try:
            obj = json.loads(
                self.execute(
                    self.prefix + ["create", "-f", "-", "-o", "json"],
                    stdin=json.dumps(self.manifest(job)),
                )
            )
            if distributed(job):
                try:
                    self.ensure_service(job, obj)
                except BackendError:
                    # Kueue admits the Job at once: without its rendezvous Service every
                    # rank would hold its GPU until the rendezvous timeout. Remove exactly
                    # the Job just created (UID precondition), then report the failure.
                    self.discard_created(obj)
                    raise
            return obj["metadata"]["name"]
        except BackendError as exc:
            raise SubmissionUnknown(str(exc)) from exc

    def discard_created(self, obj):
        options = {
            "kind": "DeleteOptions",
            "apiVersion": "v1",
            "preconditions": {"uid": obj["metadata"]["uid"]},
            "propagationPolicy": "Background",  # a raw Job DELETE otherwise orphans Pods
        }
        name = obj["metadata"]["name"]
        self.execute(
            self.prefix
            + ["delete", "--raw", f"/apis/batch/v1/namespaces/{self.namespace}/jobs/{name}"]
            + ["-f", "-"],
            stdin=json.dumps(options),
        )

    def reconcile(self, job):
        raw = self.execute(
            self.prefix
            + ["get", "job", job["body"]["attempt_id"], "--ignore-not-found", "-o", "json"]
        )
        if not raw.strip():
            return None
        obj = json.loads(raw)
        annotations = obj["metadata"].get("annotations", {})
        if (
            annotations.get("resource-advisor/job-id") != job["id"]
            or annotations.get("resource-advisor/context-signature")
            != job["body"]["context_signature"]
        ):
            raise BackendError("existing Job does not match this attempt")
        if distributed(job):
            self.ensure_service(job, obj)
        return obj["metadata"]["name"]

    def status(self, job):
        raw = self.execute(
            self.prefix
            + ["get", "job", job["body"]["external_id"], "--ignore-not-found", "-o", "json"]
        )
        pods = json.loads(
            self.execute(
                self.prefix
                + ["get", "pods", "-l", "job-name=" + job["body"]["external_id"], "-o", "json"]
            )
        )["items"]
        retained = {}
        if self.retain_termination_evidence:
            from .kubernetes_retention import observe

            retained, terminal = observe(
                self.namespace, job, json.loads(raw) if raw.strip() else None, pods
            )
            if terminal is not None:
                return Observation(**terminal, **retained)
        if not raw.strip():
            if job["state"] == State.CANCEL_REQUESTED:
                active = any(
                    p.get("status", {}).get("phase") not in {"Succeeded", "Failed"} for p in pods
                )
                return Observation(State.CANCEL_REQUESTED if active else State.CANCELED)
            raise BackendError("Job missing; disappearance is not successful completion")
        obj = json.loads(raw)
        status = obj.get("status", {})
        submitted = obj["metadata"].get("creationTimestamp")
        pods = [
            p
            for p in pods
            if any(
                owner.get("uid") == obj["metadata"].get("uid")
                for owner in p["metadata"].get("ownerReferences", [])
            )
        ]
        if distributed(job):
            terminal = any(
                c["type"] in {"Complete", "Failed"} and c["status"] == "True"
                for c in status.get("conditions", [])
            )
            if not terminal and job["state"] != State.CANCEL_REQUESTED:
                # Ranks rendezvous through the owned Service: repair a lost one while active.
                self.ensure_service(job, obj)
            return self.distributed_status(job, obj, pods, retained)
        if len(pods) > 1:
            raise BackendError("unexpected multiple Pods for a single non-retrying attempt")
        pod_status = pods[0].get("status", {}) if pods else {}
        scheduled = next(
            (
                c.get("lastTransitionTime")
                for c in pod_status.get("conditions", [])
                if c["type"] == "PodScheduled" and c["status"] == "True"
            ),
            None,
        )
        container = next(
            (
                c.get("state", {})
                for c in pod_status.get("containerStatuses", [])
                if c["name"] == "workload"
            ),
            {},
        )
        finished = container.get("terminated", {}).get("finishedAt")
        allocation = None
        if scheduled:
            main = next(
                (
                    c
                    for c in pods[0].get("spec", {}).get("containers", [])
                    if c["name"] == "workload"
                ),
                {},
            )
            requests = main.get("resources", {}).get("requests", {})
            cpu = requests.get("cpu")
            cpu = (
                number(cpu[:-1]) / 1000
                if isinstance(cpu, str) and cpu.endswith("m") and number(cpu[:-1]) is not None
                else number(cpu)
            )
            capability = job["body"]["capability"]
            context = job["body"]["candidate"]["context"]
            accelerator_count = number(requests.get(capability["resource_key"]))
            if (
                capability["device_class"] == "cpu"
                and context["allocation_mode"] == "cpu_only"
                and context["resources"]["accelerator_count"] == 0
                and capability["resource_key"] is None
                and set(requests) <= {"cpu", "memory", "ephemeral-storage"}
            ):
                # Zero is evidenced by this CPU-only Pod's observed requests.
                # Missing GPU/NPU requests in other contexts remain unknown.
                accelerator_count = 0.0
            allocation = {
                "source": "kubernetes:scheduled workload container requests",
                "accelerator_count": accelerator_count,
                "cpu": cpu,
                "memory_mib": memory_mib(requests.get("memory")),
            }
        execution_started = container.get("terminated", container.get("running", {})).get(
            "startedAt"
        )
        observed = {
            "placement": {
                "source": "kubernetes:Pod",
                "job_uid": obj["metadata"].get("uid"),
                "pod_name": pods[0]["metadata"].get("name"),
                "pod_uid": pods[0]["metadata"].get("uid"),
                "node_name": pods[0].get("spec", {}).get("nodeName"),
                "phase": pod_status.get("phase"),
            }
            if pods and pods[0].get("spec", {}).get("nodeName")
            else None,
            "admission": self.admission(obj) if self.observe_admission else None,
        }
        for condition in status.get("conditions", []):
            if condition["type"] == "Complete" and condition["status"] == "True":
                return Observation(
                    State.COLLECTING,
                    scheduled,
                    finished,
                    allocation=allocation,
                    execution_started_at=execution_started,
                    submitted_at=submitted,
                    **observed,
                    **retained,
                )
            if condition["type"] == "Failed" and condition["status"] == "True":
                return Observation(
                    State.FAILED,
                    scheduled,
                    finished,
                    condition.get("reason", "BACKEND_FAILED"),
                    allocation=allocation,
                    execution_started_at=execution_started,
                    submitted_at=submitted,
                    **observed,
                    **retained,
                )
        scheduler_reason = None
        if "running" not in container and "terminated" not in container:
            scheduler_reason = container.get("waiting", {}).get("reason")
            if not scheduler_reason:
                scheduler_reason = next(
                    (
                        c.get("reason")
                        for c in pod_status.get("conditions", [])
                        if c.get("type") == "PodScheduled" and c.get("status") == "False"
                    ),
                    None,
                )
            if not scheduler_reason and obj.get("spec", {}).get("suspend"):
                scheduler_reason = "AdmissionPending"
            if scheduler_reason and not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", scheduler_reason):
                scheduler_reason = None
        # Container exit can precede the controller's terminal Job condition.
        # Keep the attempt active until that condition arrives; requeueing here
        # would also incorrectly reapply the admission-wait deadline.
        return Observation(
            State.RUNNING if "running" in container or "terminated" in container else State.QUEUED,
            scheduled,
            scheduler_reason=scheduler_reason,
            allocation=allocation,
            execution_started_at=execution_started,
            submitted_at=submitted,
            **observed,
            **retained,
        )

    def distributed_status(self, job, obj, pods, retained):
        """All ranks of one Indexed Job; success needs every index on the allocated set.

        ``pods`` are already restricted to this Job UID. Placement off the signed
        node set (or two ranks on one host) fails the attempt and stops it.
        """
        d = distributed(job)
        allowed, n = {x["node_ref"] for x in d["nodes"]}, len(d["nodes"])
        status = obj.get("status", {})
        submitted = obj["metadata"].get("creationTimestamp")

        def container(pod):
            return next(
                (
                    c.get("state", {})
                    for c in pod.get("status", {}).get("containerStatuses", [])
                    if c["name"] == "workload"
                ),
                {},
            )

        ranks = []
        for pod in pods:
            index = pod["metadata"].get("annotations", {}).get(INDEX_ANNOTATION)
            if index is None or not index.isdigit() or not int(index) < n:
                raise BackendError("distributed Pod without a valid completion index")
            ranks.append(
                {
                    "index": int(index),
                    "pod_name": pod["metadata"].get("name"),
                    "pod_uid": pod["metadata"].get("uid"),
                    "node_name": pod.get("spec", {}).get("nodeName"),
                    "phase": pod.get("status", {}).get("phase"),
                    "state": container(pod),
                    "scheduled": next(
                        (
                            c.get("lastTransitionTime")
                            for c in pod.get("status", {}).get("conditions", [])
                            if c["type"] == "PodScheduled" and c["status"] == "True"
                        ),
                        None,
                    ),
                    "requests": next(
                        (
                            c.get("resources", {}).get("requests", {})
                            for c in pod.get("spec", {}).get("containers", [])
                            if c["name"] == "workload"
                        ),
                        {},
                    ),
                }
            )
        ranks.sort(key=lambda r: (r["index"], r["pod_name"] or ""))
        active = [r for r in ranks if r["phase"] not in {"Failed"}]
        placed = [r["node_name"] for r in active if r["node_name"]]
        misplaced = any(x not in allowed for x in placed) or len(set(placed)) != len(placed)
        complete = len(ranks) == n and {r["index"] for r in ranks} == set(range(n))
        all_scheduled = complete and all(r["scheduled"] for r in ranks)
        latest = lambda key: max(filter(None, key), default=None)  # noqa: E731
        scheduled = latest(r["scheduled"] for r in ranks) if all_scheduled else None
        started = (
            latest(
                r["state"].get("terminated", r["state"].get("running", {})).get("startedAt")
                for r in ranks
            )
            if all_scheduled
            else None
        )
        finished = latest(r["state"].get("terminated", {}).get("finishedAt") for r in ranks)
        allocation = None
        if all_scheduled:
            key = job["body"]["capability"]["resource_key"]
            counts = [number(r["requests"].get(key)) for r in ranks]
            allocation = {
                "source": "kubernetes:scheduled workload container requests (all ranks)",
                "accelerator_count": None if None in counts else sum(counts),
                "per_pod_accelerator_count": counts,
                "cpu": None,
                "memory_mib": None,
            }
        zero = next((r for r in ranks if r["index"] == 0), None)
        observed = {
            "placement": {
                "source": "kubernetes:Pod",
                "job_uid": obj["metadata"].get("uid"),
                "pod_name": zero["pod_name"],
                "pod_uid": zero["pod_uid"],
                "node_name": zero["node_name"],
                "phase": zero["phase"],
                "distributed": {
                    "placement": "same-node" if n == 1 else "multi-node",
                    "world_size": d["world_size"],
                    "ranks": [
                        {k: r[k] for k in ("index", "pod_name", "pod_uid", "node_name", "phase")}
                        for r in ranks
                    ],
                },
            }
            if zero and zero["node_name"]
            else None,
            "admission": self.admission(obj) if self.observe_admission else None,
        }
        common = dict(
            allocation=allocation,
            execution_started_at=started,
            submitted_at=submitted,
            **observed,
            **retained,
        )
        if misplaced:
            # Never let a rank keep running off the verified node set.
            self.cancel(job)
            return Observation(
                State.FAILED, scheduled, finished, "DISTRIBUTED_PLACEMENT_MISMATCH", **common
            )
        for condition in status.get("conditions", []):
            if condition["type"] == "Failed" and condition["status"] == "True":
                return Observation(
                    State.FAILED,
                    scheduled,
                    finished,
                    condition.get("reason", "BACKEND_FAILED"),
                    **common,
                )
            if condition["type"] == "Complete" and condition["status"] == "True":
                if not (
                    complete
                    and all(r["phase"] == "Succeeded" for r in ranks)
                    and set(placed) == allowed
                    and len(placed) == n
                ):
                    return Observation(
                        State.FAILED,
                        scheduled,
                        finished,
                        "DISTRIBUTED_PLACEMENT_UNVERIFIED",
                        **common,
                    )
                return Observation(State.COLLECTING, scheduled, finished, **common)
        running = any("running" in r["state"] or "terminated" in r["state"] for r in ranks)
        reason = None
        if not running:
            reason = next(
                (
                    r["state"].get("waiting", {}).get("reason")
                    for r in ranks
                    if r["state"].get("waiting", {}).get("reason")
                ),
                None,
            )
            if not reason and obj.get("spec", {}).get("suspend"):
                reason = "AdmissionPending"
            if reason and not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", reason):
                reason = None
        return Observation(
            State.RUNNING if running else State.QUEUED, scheduled, scheduler_reason=reason, **common
        )

    def distributed_result(self, job):
        """Exactly one rank (global rank 0) may emit the result, from the recorded ranks.

        Logs are read only from the Job UID and rank Pods that status() recorded as
        a complete, successful placement on the allocated node set, after checking
        that each of those Pods is still the same Pod with the same index and node.
        """
        d = distributed(job)
        allowed, n = {x["node_ref"] for x in d["nodes"]}, len(d["nodes"])
        recorded = job["body"].get("placement") or {}
        ranks = (recorded.get("distributed") or {}).get("ranks") or []
        if (
            not recorded.get("job_uid")
            or sorted(r["index"] for r in ranks) != list(range(n))
            or any(r["phase"] != "Succeeded" or not r.get("pod_uid") for r in ranks)
            or {r["node_name"] for r in ranks} != allowed
            or len({r["node_name"] for r in ranks}) != n
        ):
            raise BackendError("no recorded complete placement to bind the distributed result")
        raw = self.execute(
            self.prefix
            + ["get", "job", job["body"]["external_id"], "--ignore-not-found", "-o", "json"]
        )
        if not raw.strip():
            raise BackendError("Job missing; distributed result cannot be bound")
        uid = json.loads(raw)["metadata"].get("uid")
        if uid != recorded["job_uid"]:
            raise BackendError("Job UID differs from the recorded placement")
        live = {
            p["metadata"].get("uid"): p
            for p in json.loads(
                self.execute(
                    self.prefix
                    + [
                        "get",
                        "pods",
                        "-l",
                        "batch.kubernetes.io/controller-uid=" + str(uid),
                        "-o",
                        "json",
                    ]
                )
            )["items"]
            if any(o.get("uid") == uid for o in p["metadata"].get("ownerReferences", []))
        }
        pods = []
        for rank in ranks:
            pod = live.get(rank["pod_uid"])
            if (
                pod is None
                or pod["metadata"].get("annotations", {}).get(INDEX_ANNOTATION)
                != str(rank["index"])
                or pod.get("spec", {}).get("nodeName") != rank["node_name"]
                or pod.get("status", {}).get("phase") != "Succeeded"
            ):
                raise BackendError("rank Pod differs from the recorded placement")
            pods.append(pod)
        marker = "RESOURCE_ADVISOR_RESULT "
        found = []
        for pod in sorted(pods, key=lambda p: p["metadata"]["name"]):
            log = self.execute(
                self.prefix
                + [
                    "logs",
                    "pod/" + pod["metadata"]["name"],
                    "--container",
                    "workload",
                    "--tail=100",
                ]
            )
            if any(line.startswith(marker) for line in log.splitlines()):
                found.append(log)
        if len(found) != 1:
            raise BackendError("exactly one rank must emit the distributed result")
        return result_from_log(found[0])

    def admission(self, obj):
        """Kueue Workload owned by this exact Job UID; never matched by name alone."""
        uid = obj["metadata"].get("uid")
        try:
            items = json.loads(
                self.execute(
                    self.prefix
                    + [
                        "get",
                        "workloads.kueue.x-k8s.io",
                        "-l",
                        "kueue.x-k8s.io/job-uid=" + str(uid),
                        "-o",
                        "json",
                    ]
                )
            )["items"]
        except (BackendError, ValueError, KeyError, TypeError):
            return {"source": "kueue:Workload", "error": "UNAVAILABLE"}
        owned = [
            w
            for w in items
            if any(
                o.get("kind") == "Job" and o.get("uid") == uid and o.get("controller")
                for o in w.get("metadata", {}).get("ownerReferences", [])
            )
            and w.get("spec", {}).get("queueName") == self.local_queue
        ]
        if len(owned) != 1:
            return {"source": "kueue:Workload", "error": "NOT_FOUND" if not owned else "AMBIGUOUS"}
        workload, status = owned[0], owned[0].get("status", {})
        assignment = (status.get("admission", {}).get("podSetAssignments") or [{}])[0]
        return {
            "source": "kueue:Workload",
            "name": workload["metadata"].get("name"),
            "uid": workload["metadata"].get("uid"),
            "local_queue": self.local_queue,
            "cluster_queue": status.get("admission", {}).get("clusterQueue"),
            "flavors": assignment.get("flavors"),
            "resource_usage": assignment.get("resourceUsage"),
            "conditions": [
                {
                    "type": c.get("type"),
                    "status": c.get("status"),
                    "reason": c.get("reason"),
                    "message": str(c.get("message", ""))[:512],
                    "last_transition_time": c.get("lastTransitionTime"),
                }
                for c in status.get("conditions", [])
                if c.get("type")
                in {"QuotaReserved", "Admitted", "PodsReady", "Evicted", "Preempted", "Finished"}
            ],
        }

    def cancel(self, job):
        self.execute(
            self.prefix
            + ["delete", "job", job["body"]["external_id"], "--ignore-not-found", "--wait=false"]
            + (["--cascade=foreground"] if self.retain_termination_evidence else [])
        )

    def progress(self, job, observation):
        target = (
            "pod/" + job["body"]["termination"]["pod_name"]
            if job["body"].get("termination")
            else "job/" + job["body"]["external_id"]
        )
        log = self.execute(self.prefix + ["logs", target, "--container", "workload", "--tail=60"])
        return progress_from_log(log, job, "kubernetes:pod log tail")

    def release_termination(self, receipt):
        from .kubernetes_retention import release

        release(self, receipt)

    def result(self, job):
        if distributed(job):
            return self.distributed_result(job)
        return result_from_log(
            self.execute(
                self.prefix
                + [
                    "logs",
                    "pod/" + job["body"]["termination"]["pod_name"]
                    if job["body"].get("termination")
                    else "job/" + job["body"]["external_id"],
                    "--container",
                    "workload",
                    "--tail=100",
                ]
            )
        )


class SlurmBackend:
    """Run on the controller, or through an existing SSH alias/key (no passwords)."""

    def __init__(
        self,
        *,
        partition,
        account,
        qos,
        output_dir,
        ssh_target=None,
        qos_by_priority=None,
        native_runtimes=None,
        result_ssh_targets=None,
        training_sources=None,
        observe_progress=False,
        observe_allocation=False,
        execute=run,
    ):
        for value in [partition, account, qos]:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
                raise ValueError("partition, account and QOS must be explicit safe identifiers")
        if not re.fullmatch(r"/[A-Za-z0-9_/.-]+", output_dir):
            raise ValueError("absolute safe output directory required")
        if ssh_target and (
            ssh_target.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_@.-]+", ssh_target)
        ):
            raise ValueError("invalid SSH target")
        self.partition, self.account, self.qos = partition, account, qos
        self.qos_by_priority = priority_mapping(qos_by_priority)
        self.output_dir, self.ssh_target, self.execute = output_dir, ssh_target, execute
        self.native_runtimes = dict(native_runtimes or {})
        self.result_ssh_targets = dict(result_ssh_targets or {})
        for node, target in self.result_ssh_targets.items():
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", node) or not re.fullmatch(
                r"[A-Za-z0-9_][A-Za-z0-9_@.-]*", target
            ):
                raise ValueError("invalid result node/SSH alias")
        if not isinstance(observe_progress, bool):
            raise ValueError("progress observation must be an explicit boolean")
        self.observe_progress = observe_progress
        if not isinstance(observe_allocation, bool):
            raise ValueError("allocation observation must be an explicit boolean")
        # Opt-in: older forced-command gateways pin the five/six-column squeue format.
        self.observe_allocation = observe_allocation
        self.training_sources = dict(training_sources or {})

    def call(self, argv, *, target=None, **kwargs):
        if argv[0] == "sacct":
            # Format database epochs in an explicit zone, independent of the SSH
            # host's locale/timezone. Unqualified timestamps still remain unknown.
            argv = ["env", "TZ=UTC", "SLURM_TIME_FORMAT=%Y-%m-%dT%H:%M:%S%z", *argv]
        target = target or self.ssh_target
        if target:
            argv = [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "ConnectTimeout=5",
                target,
                shlex.join(argv),
            ]
        return self.execute(argv, **kwargs)

    def validate(self, job):
        self.script(job)

    def runtime_command(self, job):
        b = job["body"]
        variant = b["variant"]
        # Never silently run a container-qualified command on the host.
        if variant.get("image") is not None:
            raise BackendError("Slurm container execution is not configured")
        binding = self.native_runtimes.get(variant["ref"])
        if not binding or binding.get("environment_digest") != variant["environment_digest"]:
            raise BackendError("Slurm requires a qualified native runtime binding")
        for key in ("guard_path", "manifest_path"):
            path = binding.get(key, "")
            if not re.fullmatch(r"/[A-Za-z0-9_/.-]+", path) or ".." in path.split("/"):
                raise BackendError("invalid native runtime path")
        for key in ("guard_digest", "manifest_digest"):
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", binding.get(key, "")):
                raise BackendError("pinned runtime guard and manifest digests required")
        command = b.get("effective_command", variant["command"])
        if list(command) not in binding.get("commands", []):
            raise BackendError("command is not qualified by the Slurm route")
        check = (
            "printf '%s  %s\\n' "
            + shlex.join([binding["guard_digest"][7:], binding["guard_path"]])
            + " | sha256sum --check --status\n"
        )
        guarded = [
            "/usr/bin/python3",
            "-I",
            binding["guard_path"],
            "--manifest",
            binding["manifest_path"],
            "--manifest-digest",
            binding["manifest_digest"],
            "--environment-digest",
            binding["environment_digest"],
            "--",
            *command,
        ]
        return check, guarded

    def script(self, job):
        if distributed(job):
            # No verified multi-node Slurm launch shape exists yet; never guess one.
            raise BackendError("DISTRIBUTED_SLURM_UNSUPPORTED")
        b = job["body"]
        training = None
        if b["spec"]["identity"]["task_type"] == "training":
            training = self.training_binding(job)
        qos = policy_priority(job, self.qos_by_priority, self.qos)
        from .scheduling import verify_adapter_plan

        try:
            verify_adapter_plan(
                job, {"partition": self.partition, "account": self.account, "qos": qos}
            )
        except ValueError as exc:
            raise BackendError(str(exc)) from exc
        check, command = self.runtime_command(job)
        r = b["candidate"]["context"]["resources"]
        if r["host_cpu"] != int(r["host_cpu"]):
            raise BackendError("Slurm CPU count must be an integer")
        node = b["capability"]["node_ref"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", node):
            raise BackendError("invalid qualified node reference")
        target = pool_target(job)
        if target:
            # Slurm chooses within an operator-verified exclusive feature; the
            # allocated node is checked against the members before any result use.
            constraint = target.get("slurm_constraint") or ""
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", constraint) or not target.get(
                "slurm_constraint_exclusive_evidence_ref"
            ):
                raise BackendError("Slurm pool requires a verified exclusive feature constraint")
            placement = f"--constraint={constraint}"
        else:
            placement = f"--nodelist={node}"
        seconds = b.get("execution_limits", b["spec"]["execution"])["max_run_seconds"]
        directives = [
            f"--job-name={b['attempt_id']}",
            f"--comment=resource-advisor:{job['id']}",
            f"--partition={self.partition}",
            f"--account={self.account}",
            f"--qos={qos}",
            placement,
            "--nodes=1",
            "--ntasks=1",
            "--chdir=/",
            f"--cpus-per-task={int(r['host_cpu'])}",
            f"--mem={r['host_memory_mib']}M",
            f"--time={max(1, (seconds + 59) // 60)}",
            f"--output={self.output_dir}/{b['attempt_id']}.log",
            "--export=NONE",
        ]
        if r["accelerator_count"]:
            gres = b["capability"]["resource_key"]
            if not gres or not re.fullmatch(r"(?:gpu|npu)(?::[A-Za-z0-9_.-]+)?", gres):
                raise BackendError("verified Slurm GRES is required")
            directives.append(f"--gres={gres}:{r['accelerator_count']}")
        env = identity_environment(job)
        if training:
            if r["accelerator_count"]:
                raise BackendError("Slurm training is qualified only without accelerators")
            env["RA_TRAINING_BINDING"] = json.dumps(training, separators=(",", ":"))
        return (
            "#!/bin/bash\n"
            + "\n".join("#SBATCH " + d for d in directives)
            + "\nset -euo pipefail\n"
            + "\n".join(f"export {k}={shlex.quote(v)}" for k, v in env.items())
            + "\n"
            + check
            + "exec "
            + shlex.join(["srun", "--export=ALL", *command])
            + "\n"
        )

    def training_binding(self, job):
        """Operator route for the CPU host-copy runner; every other training fails closed."""
        from .contracts import signature
        from .training import TrainingIsolation

        b = job["body"]
        route = self.training_sources.get((b.get("training_isolation") or {}).get("ref"))
        if not route:
            raise BackendError("Slurm checkpoint isolation is not qualified")
        try:
            binding = TrainingIsolation.model_validate(b["training_isolation"])
        except ValueError as exc:
            raise BackendError("invalid Slurm training binding") from exc
        safe = r"/[A-Za-z0-9_/.-]+"
        command = list(b.get("effective_command", b["variant"]["command"]))
        if (
            binding.backend != "slurm"
            or binding.device_class != "cpu"
            or set(route) != {"binding_digest", "source_dir", "work_dir"}
            or route["binding_digest"] != signature(binding)
            or not all(
                isinstance(route[k], str)
                and re.fullmatch(safe, route[k])
                and ".." not in route[k].split("/")
                for k in ("source_dir", "work_dir")
            )
            or command[-4:]
            != ["--host-source", route["source_dir"], "--host-work", route["work_dir"]]
        ):
            raise BackendError("invalid Slurm training source binding")
        return binding.model_dump(mode="json")

    def submit(self, job):
        script = self.script(job)
        try:
            output = self.call(["sbatch", "--parsable"], stdin=script).strip()
        except BackendError as exc:
            raise SubmissionUnknown(str(exc)) from exc
        if not re.fullmatch(r"[0-9]+(?:;[A-Za-z0-9_.-]+)?", output):
            raise SubmissionUnknown("sbatch returned no unambiguous job ID")
        return output.split(";", 1)[0]

    def reconcile(self, job):
        name = job["body"]["attempt_id"]
        # sacct failure is UNKNOWN, never proof of absence. No SlurmDBD means this
        # backend cannot safely recover every response-loss case.
        queued = self.call(
            [
                "squeue",
                "--noheader",
                "--account",
                self.account,
                "--partition",
                self.partition,
                "--name",
                name,
                "--format=%i|%j|%a|%P",
            ]
        )
        accounted = self.call(
            [
                "sacct",
                "--noheader",
                "--parsable2",
                "--name",
                name,
                "--accounts",
                self.account,
                "--partition",
                self.partition,
                "--starttime",
                job["body"]["created_at"][:19],
                "--format=JobIDRaw,JobName%100,Account%100,Partition%100",
            ]
        )
        ids = {
            line.split("|")[0]
            for line in (queued + "\n" + accounted).splitlines()
            if len(line.split("|")) == 4
            and line.split("|")[1] == name
            and line.split("|")[2] == self.account
            and line.split("|")[3] == self.partition
            and re.fullmatch(r"\d+", line.split("|")[0])
        }
        if len(ids) > 1:
            raise BackendError("multiple external jobs match one attempt")
        return next(iter(ids), None)

    @staticmethod
    def external_id(job):
        jid = job["body"].get("external_id")
        if not isinstance(jid, str) or not re.fullmatch(r"[1-9][0-9]*", jid):
            raise BackendError("invalid Slurm external ID")
        return jid

    def owned_record(self, output, job, *, width, required=True):
        jid = self.external_id(job)
        records = [line.split("|") for line in output.splitlines() if line.split("|")[0] == jid]
        if not records and not required:
            return None
        if len(records) != 1 or len(records[0]) != width:
            raise BackendError("unambiguous Slurm allocation required")
        record = records[0]
        if record[1:4] != [job["body"]["attempt_id"], self.account, self.partition]:
            raise BackendError("Slurm allocation does not match the attempt owner")
        return record

    def failure_reason(self, output, job, allocation):
        """Keep a contained step OOM when srun leaves the parent as FAILED."""
        state = allocation[4]
        if state != "FAILED" or not all(_aware(value) for value in allocation[6:8]):
            return state
        start, end = (datetime.fromisoformat(value) for value in allocation[6:8])
        if end < start:
            return state
        jid = self.external_id(job)
        for line in output.splitlines():
            step = line.split("|")
            if (
                len(step) != 10
                or not re.fullmatch(re.escape(jid) + r"\.(?:[0-9]+|batch|extern)", step[0])
                or step[2] != self.account
                or step[3] not in {"", self.partition}
                or step[4] != "OUT_OF_MEMORY"
                or not all(_aware(value) for value in step[6:8])
            ):
                continue
            step_start, step_end = (datetime.fromisoformat(value) for value in step[6:8])
            if start <= step_start <= step_end <= end:
                return "OUT_OF_MEMORY"
        return state

    def status(self, job):
        jid = self.external_id(job)
        # squeue --jobs exits nonzero after Slurm purges a completed ID from its
        # live cache. A successful account-scoped listing proves it is absent
        # there; sacct still decides completion. Transport errors still propagate.
        queued = self.call(
            [
                "squeue",
                "--noheader",
                "--account",
                self.account,
                "--partition",
                self.partition,
                "--format=%i|%j|%a|%P|%T|%r" + ("|%N" if self.observe_allocation else ""),
            ]
        )
        # Older command wrappers may still emit five columns; reason remains unknown.
        width = (
            5
            if queued.strip()
            and all(len(line.split("|")) == 5 for line in queued.splitlines() if line.strip())
            else 7
            if self.observe_allocation
            else 6
        )
        record = self.owned_record(queued, job, width=width, required=False)
        if record:
            running = record[4] in {"RUNNING", "COMPLETING"}
            observation = Observation(
                State.RUNNING if running else State.QUEUED,
                scheduler_reason=record[5]
                if width >= 6
                and record[4] == "PENDING"
                and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", record[5])
                else None,
            )
            if running and width == 7 and re.fullmatch(r"[A-Za-z0-9_.-]+", record[6]):
                observation.placement = {
                    "source": "slurm:squeue",
                    "job_id": jid,
                    "node_name": record[6],
                    "state": record[4],
                }
                # Accounting of the same owned ID gives the actual running allocation.
                active = self.owned_record(self.accounting(jid), job, width=10)
                start, tres, submitted = active[6], active[8], active[9]
                observation.started_at = start if _aware(start) else None
                observation.submitted_at = submitted if _aware(submitted) else None
                observation.allocation = self.allocation(job, tres)
            return observation
        output = self.accounting(jid)
        parts = self.owned_record(output, job, width=10)
        state, exitcode, start, end, tres, submitted = parts[4:]
        # The command requests UTC offsets. Unsupported/missing values stay unknown.
        start = start if _aware(start) else None
        end = end if _aware(end) else None
        allocation = self.allocation(job, tres)
        submitted = submitted if _aware(submitted) else None
        if state == "COMPLETED" and exitcode == "0:0":
            return Observation(
                State.COLLECTING, start, end, allocation=allocation, submitted_at=submitted
            )
        if state.startswith("CANCELLED"):
            return Observation(
                State.CANCELED, start, end, allocation=allocation, submitted_at=submitted
            )
        if state in {
            "FAILED",
            "TIMEOUT",
            "OUT_OF_MEMORY",
            "NODE_FAIL",
            "PREEMPTED",
            "BOOT_FAIL",
        }:
            return Observation(
                State.FAILED,
                start,
                end,
                self.failure_reason(output, job, parts),
                allocation=allocation,
                submitted_at=submitted,
            )
        raise BackendError("accounting has not confirmed a terminal state")

    @staticmethod
    def allocation(job, tres):
        allocation = slurm_allocation(tres, job["body"]["capability"].get("resource_key"))
        capability, context = job["body"]["capability"], job["body"]["candidate"]["context"]
        if (
            allocation
            and allocation["accelerator_count"] is None
            and capability["device_class"] == "cpu"
            and capability["resource_key"] is None
            and context["allocation_mode"] == "cpu_only"
            and context["resources"]["accelerator_count"] == 0
        ):
            # Zero is evidenced by this CPU-only job's AllocTRES listing no GRES.
            allocation["accelerator_count"] = 0.0
        return allocation

    def accounting(self, jid):
        return self.call(
            [
                "sacct",
                "--noheader",
                "--parsable2",
                "--duplicates",
                "--accounts",
                self.account,
                "--partition",
                self.partition,
                "--jobs",
                jid,
                "--format=JobIDRaw,JobName%100,Account%100,Partition%100,State%64,ExitCode,Start,End,AllocTRES%200,Submit",
            ]
        )

    def cancel(self, job):
        jid = self.external_id(job)
        # Ask the controller to apply all filters at cancellation, rather than
        # relying only on the worker's earlier ownership observation.
        self.call(
            [
                "scancel",
                "--ctld",
                "--account",
                self.account,
                "--partition",
                self.partition,
                "--name",
                job["body"]["attempt_id"],
                jid,
            ]
        )

    def output_target(self, node):
        if not self.result_ssh_targets:
            return None
        target = self.result_ssh_targets.get(node)
        if not target:
            raise BackendError("result node has no authorized transport")
        return target

    def progress(self, job, observation):
        # status() has just matched the attempt owner; the output read is bounded,
        # path-fixed and still checked again by the forced-command gateway.
        # After the job leaves squeue, the node comes from the saved running placement.
        node = (
            (observation.placement if observation else None) or job["body"].get("placement") or {}
        ).get("node_name")
        if node is None and self.result_ssh_targets:
            raise BackendError("running node is unknown")
        path = f"{self.output_dir}/{job['body']['attempt_id']}.log"
        log = self.call(["tail", "-c", "16384", "--", path], target=self.output_target(node))
        return progress_from_log(log, job, "slurm:output tail")

    def result(self, job):
        # Check ownership even when the controller can read a shared filesystem.
        jid = self.external_id(job)
        output = self.call(
            [
                "sacct",
                "--noheader",
                "--parsable2",
                "--duplicates",
                "--accounts",
                self.account,
                "--partition",
                self.partition,
                "--jobs",
                jid,
                "--format=JobIDRaw,JobName%100,Account%100,Partition%100,NodeList%100,State%64,ExitCode",
            ]
        )
        _, _, _, _, node, state, exitcode = self.owned_record(output, job, width=7)
        target = pool_target(job)
        allowed = (
            {m["node_ref"] for m in target["members"]}
            if target
            else {job["body"]["capability"]["node_ref"]}
        )
        if node not in allowed or state != "COMPLETED" or exitcode != "0:0":
            raise BackendError("result allocation does not match the completed attempt")
        path = f"{self.output_dir}/{job['body']['attempt_id']}.log"
        return result_from_log(
            self.call(["tail", "-c", "65537", "--", path], target=self.output_target(node))
        )


def _aware(value):
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False
