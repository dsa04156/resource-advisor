"""Separate adapters. Kubernetes admission and Slurm scheduling stay independent."""

import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .contracts import State


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


class SchedulerBackend(Protocol):
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


def identity_environment(job):
    b = job["body"]
    return {
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
        execute=run,
    ):
        if not namespace or not local_queue or not node_selector:
            raise ValueError("explicit namespace, LocalQueue and allowed node pool are required")
        self.namespace, self.local_queue, self.node_selector = namespace, local_queue, node_selector
        self.execute = execute
        self.runtime_bundles = runtime_bundles or {}
        self.prefix = ["kubectl"]
        if kubeconfig:
            self.prefix += ["--kubeconfig", kubeconfig]
        if context:
            self.prefix += ["--context", context]
        self.prefix += ["--namespace", namespace]

    def manifest(self, job):
        b = job["body"]
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
        bundle = self.runtime_bundles.get(b["variant"]["ref"])
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
        return manifest

    def submit(self, job):
        try:
            obj = json.loads(
                self.execute(
                    self.prefix + ["create", "-f", "-", "-o", "json"],
                    stdin=json.dumps(self.manifest(job)),
                )
            )
            return obj["metadata"]["name"]
        except BackendError as exc:
            raise SubmissionUnknown(str(exc)) from exc

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
        if not raw.strip():
            if job["state"] == State.CANCEL_REQUESTED:
                active = any(
                    p.get("status", {}).get("phase") not in {"Succeeded", "Failed"} for p in pods
                )
                return Observation(State.CANCEL_REQUESTED if active else State.CANCELED)
            raise BackendError("Job missing; disappearance is not successful completion")
        obj = json.loads(raw)
        status = obj.get("status", {})
        pods = [
            p
            for p in pods
            if any(
                owner.get("uid") == obj["metadata"].get("uid")
                for owner in p["metadata"].get("ownerReferences", [])
            )
        ]
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
        for condition in status.get("conditions", []):
            if condition["type"] == "Complete" and condition["status"] == "True":
                return Observation(State.COLLECTING, scheduled, finished)
            if condition["type"] == "Failed" and condition["status"] == "True":
                return Observation(
                    State.FAILED,
                    scheduled,
                    finished,
                    condition.get("reason", "BACKEND_FAILED"),
                )
        return Observation(State.RUNNING if "running" in container else State.QUEUED, scheduled)

    def cancel(self, job):
        self.execute(
            self.prefix
            + ["delete", "job", job["body"]["external_id"], "--ignore-not-found", "--wait=false"]
        )

    def result(self, job):
        return result_from_log(
            self.execute(
                self.prefix
                + [
                    "logs",
                    "job/" + job["body"]["external_id"],
                    "--container",
                    "workload",
                    "--tail=100",
                ]
            )
        )


class SlurmBackend:
    """Run on the controller, or through an existing SSH alias/key (no passwords)."""

    def __init__(self, *, partition, account, qos, output_dir, ssh_target=None, execute=run):
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
        self.output_dir, self.ssh_target, self.execute = output_dir, ssh_target, execute

    def call(self, argv, **kwargs):
        if argv[0] == "sacct":
            # Format database epochs in an explicit zone, independent of the SSH
            # host's locale/timezone. Unqualified timestamps still remain unknown.
            argv = ["env", "TZ=UTC", "SLURM_TIME_FORMAT=%Y-%m-%dT%H:%M:%S%z", *argv]
        if self.ssh_target:
            argv = [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "ConnectTimeout=5",
                self.ssh_target,
                shlex.join(argv),
            ]
        return self.execute(argv, **kwargs)

    def script(self, job):
        b = job["body"]
        r = b["candidate"]["context"]["resources"]
        if r["host_cpu"] != int(r["host_cpu"]):
            raise BackendError("Slurm CPU count must be an integer")
        node = b["capability"]["node_ref"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", node):
            raise BackendError("invalid qualified node reference")
        seconds = b.get("execution_limits", b["spec"]["execution"])["max_run_seconds"]
        directives = [
            f"--job-name={b['attempt_id']}",
            f"--comment=resource-advisor:{job['id']}",
            f"--partition={self.partition}",
            f"--account={self.account}",
            f"--qos={self.qos}",
            f"--nodelist={node}",
            "--nodes=1",
            "--ntasks=1",
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
        return (
            "#!/bin/bash\n"
            + "\n".join("#SBATCH " + d for d in directives)
            + "\nset -euo pipefail\n"
            + "\n".join(f"export {k}={shlex.quote(v)}" for k, v in env.items())
            + "\nexec "
            + shlex.join(
                ["srun", "--export=ALL", *b.get("effective_command", b["variant"]["command"])]
            )
            + "\n"
        )

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
        queued = self.call(["squeue", "--noheader", "--name", name, "--format=%i|%j"])
        accounted = self.call(
            [
                "sacct",
                "--noheader",
                "--parsable2",
                "--name",
                name,
                "--starttime",
                job["body"]["created_at"][:19],
                "--format=JobIDRaw,JobName%100",
            ]
        )
        ids = {
            line.split("|")[0]
            for line in (queued + "\n" + accounted).splitlines()
            if len(line.split("|")) >= 2
            and line.split("|")[1] == name
            and re.fullmatch(r"\d+", line.split("|")[0])
        }
        if len(ids) > 1:
            raise BackendError("multiple external jobs match one attempt")
        return next(iter(ids), None)

    def status(self, job):
        jid = job["body"]["external_id"]
        if not re.fullmatch(r"\d+", jid):
            raise BackendError("invalid Slurm external ID")
        queued = self.call(["squeue", "--noheader", "--jobs", jid, "--format=%T"])
        states = queued.strip().splitlines()
        if states:
            return Observation(
                State.RUNNING if states[0] in {"RUNNING", "COMPLETING"} else State.QUEUED
            )
        output = self.call(
            [
                "sacct",
                "--noheader",
                "--parsable2",
                "--jobs",
                jid,
                "--format=JobIDRaw,State,ExitCode,Start,End",
            ]
        )
        for line in output.splitlines():
            parts = line.split("|")
            if len(parts) < 5 or parts[0] != jid:
                continue
            _, state, exitcode, start, end = parts[:5]
            # Slurm timestamps lack an offset. Do not invent UTC or duration.
            start = start if _aware(start) else None
            end = end if _aware(end) else None
            if state == "COMPLETED" and exitcode == "0:0":
                return Observation(State.COLLECTING, start, end)
            if state.startswith("CANCELLED"):
                return Observation(State.CANCELED, start, end)
            if state in {
                "FAILED",
                "TIMEOUT",
                "OUT_OF_MEMORY",
                "NODE_FAIL",
                "PREEMPTED",
                "BOOT_FAIL",
            }:
                return Observation(State.FAILED, start, end, state)
        raise BackendError("accounting has not confirmed a terminal state")

    def cancel(self, job):
        self.call(["scancel", job["body"]["external_id"]])

    def result(self, job):
        path = f"{self.output_dir}/{job['body']['attempt_id']}.log"
        return result_from_log(self.call(["tail", "-n", "100", "--", path]))


def _aware(value):
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False
