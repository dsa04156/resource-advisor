"""Bounded Round Robin / v2 Profile Reuse comparison for one frozen Digits MLP.

The plan and eight-forward native qualifications are prepared separately. CUDA
and direct Intel NPU routes may share this group; different-model NPU templates
belong to separate coverage groups. This script never changes production policy.
"""

import argparse
import copy
import csv
import json
import math
import os
import random
import statistics
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from jct_native import (
    TERMINAL,
    digest,
    normalize_kubernetes,
    normalize_slurm,
    parse_native_time,
    result_line,
    validate_boundaries,
    validate_result,
)
from run_jct_comparison import NativeTransport, apply_receipt, import_private, stop_owned, write


def result_plan(plan, node):
    return {
        **plan,
        "kernel_sha256": node.get(
            "kernel_sha256",
            node["source_sha256"] if node["runtime"] == "openvino" else plan["kernel_sha256"],
        ),
    }


def validate_runtime_result(result, plan, node, rounds):
    """Require the original fixture/quality and proof of direct runtime execution."""
    validate_result(result, result_plan(plan, node), rounds)
    if (
        result.get("evidence_kind") != "hardware"
        or result.get("arch") != node["arch"]
        or result.get("batch_size") != 256
        or not result.get("runtime_versions")
        or not math.isfinite(result["elapsed_seconds"])
        or result["elapsed_seconds"] <= 0
    ):
        raise ValueError("unknown native runtime identity or measured work")
    if node["runtime"] == "openvino":
        devices = result.get("execution_devices", [])
        if not devices or any(d != "NPU" and not d.startswith("NPU.") for d in devices):
            raise ValueError("CPU/AUTO/HETERO fallback cannot qualify direct NPU")
        if (
            result.get("source_sha256") != node["source_sha256"]
            or result.get("quality_passed") is not True
            or result.get("numerical_close") is not True
            or not result.get("model_sha256")
            or not result.get("input_sha256")
        ):
            raise ValueError("direct NPU source/model/input/numerical quality unqualified")


def qualify_receipt(item, receipt, plan, node, log, *, require_success=True):
    """Recheck saved native ownership, route, fixed work and original quality."""
    if node["backend"] == "kubernetes":
        normalizer = normalize_openvino if node["runtime"] == "openvino" else normalize_kubernetes
        observed = normalizer(
            receipt["job"],
            [receipt["pod"]],
            item,
            node,
            node.get("qualification_experiment_id", plan["experiment_id"])
            if item["requested_work"] == 8
            else plan["experiment_id"],
            log,
            result_plan(plan, node),
        )
        source_names = {
            v["configMap"]["name"]
            for v in receipt["pod"]["spec"].get("volumes", [])
            if "configMap" in v
        }
        if node.get("source_configmap", plan.get("source_configmap")) not in source_names:
            raise ValueError("native Pod used a different frozen source")
    else:
        observed = normalize_slurm(
            [receipt["native_accounting"]], item, log, result_plan(plan, node)
        )
    if observed is None or (require_success and observed["termination"]["exitCode"] != 0):
        raise ValueError("native qualification failed or incomplete")
    if observed["termination"]["exitCode"] == 0:
        validate_runtime_result(observed["result"], plan, node, item["requested_work"])
    return observed


def normalize_openvino(job, pods, item, node, experiment, log, plan):
    """Direct-NPU adapter; its qualified CLI includes an optional blob export flag."""
    if len(pods) != 1:
        raise ValueError("ambiguous native NPU Pod count")
    pod = pods[0]
    statuses = pod.get("status", {}).get("containerStatuses", [])
    if len(statuses) != 1 or statuses[0].get("restartCount", 0):
        raise ValueError("native NPU container/retry ambiguous")
    ended = statuses[0].get("state", {}).get("terminated")
    if ended is None:
        return None
    metadata = job["metadata"]
    owners = [o for o in pod["metadata"].get("ownerReferences", []) if o.get("kind") == "Job"]
    if (
        metadata["name"] != item["attempt_id"]
        or metadata.get("labels", {}).get("resource-advisor/experiment") != experiment
        or len(owners) != 1
        or owners[0].get("uid") != metadata["uid"]
        or owners[0].get("name", metadata["name"]) != metadata["name"]
        or job["spec"].get("backoffLimit") != 0
    ):
        raise ValueError("native NPU Job/Pod ownership or retry mismatch")
    for spec in (job["spec"]["template"]["spec"], pod["spec"]):
        containers = spec["containers"]
        if len(containers) != 1:
            raise ValueError("ambiguous native NPU containers")
        container = containers[0]
        command = container["command"]
        if (
            spec.get("nodeSelector", {}).get("kubernetes.io/hostname") != node["node"]
            or container["image"] != node["image"]
            or spec.get("runtimeClassName") != node.get("runtime_class")
            or command.count("--rounds") != 1
            or command[command.index("--rounds") + 1] != str(item["requested_work"])
            or any(
                container["resources"][k].get(node["resource_key"]) != "1"
                for k in ("requests", "limits")
            )
        ):
            raise ValueError("native NPU image/node/resource/fixed-work mismatch")
    if pod["spec"].get("nodeName") != node["node"]:
        raise ValueError("native NPU executed on wrong node")
    scheduled = [
        c["lastTransitionTime"]
        for c in pod["status"].get("conditions", [])
        if c["type"] == "PodScheduled" and c["status"] == "True"
    ]
    if len(scheduled) != 1:
        raise ValueError("native NPU allocation start unknown")
    accepted, started, finished = (
        parse_native_time(v)
        for v in (metadata["creationTimestamp"], scheduled[0], ended["finishedAt"])
    )
    validate_boundaries(accepted, started, finished)
    result = result_line(log) if ended["exitCode"] == 0 else None
    if result is not None:
        validate_result(result, plan, item["requested_work"])
    return dict(
        attempt_id=item["attempt_id"],
        job=job,
        pod=pod,
        result=result,
        termination=ended,
        accepted_at=accepted,
        scheduled_at=started,
        finished_at=finished,
        gpu_seconds=finished - started,
        native_state="COMPLETED" if ended["exitCode"] == 0 else "FAILED",
        log_sha256=digest(log.encode()),
    )


def freeze_microprofiles(plan, qualifications):
    """Original v2 estimate: scaled microprofile compute plus measured overhead."""
    validate_plan(plan)
    by_candidate = {q["candidate_ref"]: q for q in qualifications}
    if len(by_candidate) != len(qualifications) or set(by_candidate) != {
        n["ref"] for n in plan["pool"]
    }:
        raise ValueError("exactly one native microprofile per compatible candidate required")
    profiles = {}
    for node in plan["pool"]:
        job = by_candidate[node["ref"]]
        if job["requested_work"] != 8 or job["backend"] != node["backend"]:
            raise ValueError("changed frozen native microprofile identity")
        receipt = job["native_receipt"]
        log = "POOL_INFERENCE_RESULT " + json.dumps(receipt["result"]) + "\n"
        observed = qualify_receipt(job, receipt, plan, node, log)
        result = observed["result"]
        compute = result["elapsed_seconds"] / 8 * 16384
        overhead = max(0.0, observed["gpu_seconds"] - result["elapsed_seconds"])
        profiles[node["ref"]] = {
            "source_attempt": job["attempt_id"],
            "predicted_service_seconds": compute + overhead,
            "scaled_compute_seconds": compute,
            "startup_release_seconds": overhead,
            "observed_reservation_seconds": observed["gpu_seconds"],
            "accepted_at": observed["accepted_at"],
            "started_at": observed["scheduled_at"],
            "finished_at": observed["finished_at"],
            "runtime_versions": result["runtime_versions"],
            "model_sha256": result.get("model_sha256"),
            "input_sha256": result.get("input_sha256"),
            "compiled_sha256": result.get("compiled_sha256"),
            "scope": "one native eight-forward microprofile; linear scaling is a prediction, not measured full-work performance",
        }
    return profiles


def select_assignments(plan, profiles, arm):
    """Freeze v2 choices with static backlog; controls consume no live queue."""
    if arm not in {"baseline", "reuse"}:
        raise ValueError("only Round Robin and v2 Profile Reuse arms are supported")
    backlog = dict.fromkeys((n["ref"] for n in plan["pool"]), 0.0)
    assignments = []
    for index in range(plan["jobs_per_arm"]):
        if arm == "baseline":
            ref = plan["pool"][index % len(plan["pool"])]["ref"]
        else:
            ref = min(
                backlog,
                key=lambda ref: (backlog[ref] + profiles[ref]["predicted_service_seconds"], ref),
            )
            backlog[ref] += profiles[ref]["predicted_service_seconds"]
        assignments.append(ref)
    return assignments


def validate_plan(plan):
    """Require equal full work, one physical-candidate baseline, and bounded runs."""
    pool = plan["pool"]
    if not pool or len({n["ref"] for n in pool}) != len(pool):
        raise ValueError("unique compatible physical candidates required")
    if len({(n["backend"], n["node"], n["resource_key"]) for n in pool}) != len(pool):
        raise ValueError("duplicate physical candidate")
    if any(n.get("runtime") not in {"cuda", "openvino"} for n in pool):
        raise ValueError("compatible MLP runtime must be CUDA or direct OpenVINO NPU")
    if plan["qualification_rounds"] != 8 or plan["main_rounds"] != 16384:
        raise ValueError("freeze eight-forward microprofiles and full 16384-forward work")
    if plan["jobs_per_arm"] != len(pool):
        raise ValueError("both arms must contain one job per eligible physical candidate")
    limits = plan["limits"]
    if not 0 < limits["per_job_seconds"] <= 600 or limits["protocol_seconds"] != 1800:
        raise ValueError("jobs bounded at 600 seconds and 1800-second protocol required")
    for node in pool:
        if node["backend"] not in {"kubernetes", "slurm"}:
            raise ValueError("native Kubernetes or Slurm route required")
        if node["runtime"] == "openvino" and node["backend"] != "kubernetes":
            raise ValueError("direct Intel NPU requires its qualified Kubernetes route")
        for key in ("qualification_file", "resource_key", "arch"):
            if not node.get(key):
                raise ValueError("missing qualified route: " + key)
        if node["backend"] == "kubernetes" and not all(
            node.get(k) for k in ("image", "template_file")
        ):
            raise ValueError("immutable source and qualified native template required")
        if node["backend"] == "kubernetes" and not node.get(
            "source_configmap", plan.get("source_configmap")
        ):
            raise ValueError("immutable source ConfigMap required")
    return plan


def archive_read(directory, name):
    relative = Path(name)
    path = (directory / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(directory.resolve()):
        raise ValueError("archive input must stay within experiment directory")
    return path.read_bytes()


def load_inputs(directory):
    """Consume hashed raw microprofile logs before freezing any main choices."""
    plan = validate_plan(json.loads((directory / "plan.json").read_text()))
    jobs, hashes = [], {"plan.json": digest((directory / "plan.json").read_bytes())}
    for node in plan["pool"]:
        raw = archive_read(directory, node["qualification_file"])
        hashes[node["qualification_file"]] = digest(raw)
        job = json.loads(raw)
        receipt = job["native_receipt"]
        log_file = receipt["log_file"]
        log = archive_read(directory, log_file)
        if digest(log) != receipt["log_sha256"] or result_line(log.decode()) != receipt["result"]:
            raise ValueError("changed raw qualification log")
        hashes[log_file] = digest(log)
        if node["backend"] == "kubernetes":
            hashes[node["template_file"]] = digest(archive_read(directory, node["template_file"]))
        jobs.append(job)
    profiles = freeze_microprofiles(plan, jobs)
    return plan, jobs, profiles, hashes


class ProfileTransport(NativeTransport):
    """Reuse native collection/submission without changing immutable old sources."""

    def __init__(self, private, directory, plan):
        super().__init__(private, directory, plan)
        self.remote_directory = plan.get("remote_directory", self.remote_directory)

    def preflight(self):
        sources = {}
        nodes = self.kube("get", "nodes", "-o", "json")
        by_name = {n["metadata"]["name"]: n for n in nodes["items"]}
        for node in self.plan["pool"]:
            if node["backend"] != "kubernetes":
                continue
            name = node.get("source_configmap", self.plan.get("source_configmap"))
            if name not in sources:
                sources[name] = self.kube("get", "configmap", name, "-o", "json")
            source = sources[name]
            filename = node.get(
                "source_file",
                "pool_inference_openvino.py" if node["runtime"] == "openvino" else "workload.py",
            )
            if (
                source.get("immutable") is not True
                or digest(source["data"]["fixture.json"].encode()) != self.plan["fixture_sha256"]
                or digest(source["data"][filename].encode())
                != node.get("source_sha256", self.plan["source_sha256"])
                or int(by_name[node["node"]]["status"]["allocatable"].get(node["resource_key"], 0))
                != node["nominal_slots"]
            ):
                raise ValueError("immutable native source or declared capacity changed")
        transferred = None
        if any(n["backend"] == "slurm" for n in self.plan["pool"]):
            code = (
                "import pathlib,hashlib,json; print(json.dumps({n:hashlib.sha256((pathlib.Path("
                + repr(self.remote_directory)
                + ")/n).read_bytes()).hexdigest() for n in "
                + repr(list(self.plan["transfer_checksums"]))
                + "}))"
            )
            transferred = json.loads(self.private.remote("slurm_orin", code, timeout=20))
            if transferred != self.plan["transfer_checksums"]:
                raise ValueError("existing immutable Slurm source changed")
        return dict(at=time.time(), sources=sources, nodes=nodes, transfer_checksums=transferred)

    def receipt(self, item, node, snapshot):
        if node["backend"] == "kubernetes":
            jobs = [j for j in snapshot["jobs"] if j["metadata"]["name"] == item["attempt_id"]]
            if len(jobs) != 1:
                return None
            job = jobs[0]
            pods = [
                p
                for p in snapshot["pods"]
                if any(
                    o.get("kind") == "Job" and o.get("uid") == job["metadata"]["uid"]
                    for o in p["metadata"].get("ownerReferences", [])
                )
            ]
            if not any(
                s.get("state", {}).get("terminated")
                for p in pods
                for s in p.get("status", {}).get("containerStatuses", [])
            ):
                return None
            log = self.kube("logs", "job/" + item["attempt_id"], structured=False)
            raw = {"job": job, "pods": pods}
            if len(pods) != 1:
                item["partial_native_evidence"] = raw
                raise ValueError("ambiguous native Pod count")
            receipt = {"job": job, "pod": pods[0]}
        else:
            rows = [
                r for r in snapshot["accounting_rows"] if r["native_id"] == item.get("native_id")
            ]
            if not rows or not any(r["state"].split()[0].split("+")[0] in TERMINAL for r in rows):
                return None
            log = self.private.remote(
                "slurm_orin",
                "from pathlib import Path;print(Path("
                + repr(self.remote_directory + "/" + item["attempt_id"] + ".log")
                + ").read_text(),end='')",
                timeout=25,
            )
            raw = {"native_accounting": rows}
            if len(rows) != 1:
                item["partial_native_evidence"] = raw
                raise ValueError("ambiguous native accounting identity")
            receipt = {"native_accounting": rows[0]}
        filename = "logs/" + item["attempt_id"] + ".log"
        (self.directory / filename).write_text(log)
        item["partial_native_evidence"] = {
            **raw,
            "log_file": filename,
            "log_sha256": digest(log.encode()),
            "native_snapshot_ref": snapshot["snapshot_ref"],
        }
        observed = qualify_receipt(item, receipt, self.plan, node, log, require_success=False)
        observed.update(
            log_file=filename,
            native_snapshot_ref=snapshot["snapshot_ref"],
            native_snapshot_sha256=snapshot["snapshot_sha256"],
        )
        return observed

    def stop_owned(self, known):
        return stop_owned(self, self.directory, known, self.plan)


def pool_idle(snapshot, pool):
    """Fail closed on observed active accelerator demand or unreleased quotas."""
    if snapshot.get("errors"):
        return False
    for node in pool:
        for pod in [*snapshot.get("all_pods", []), *snapshot.get("pods", [])]:
            if pod.get("status", {}).get("phase") in {"Succeeded", "Failed"}:
                continue
            spec = pod.get("spec", {})
            route = spec.get("nodeName") or spec.get("nodeSelector", {}).get(
                "kubernetes.io/hostname"
            )
            if (not route or route == node["node"]) and any(
                c.get("resources", {}).get("requests", {}).get(node["resource_key"])
                for c in spec.get("containers", [])
            ):
                return False
        for job in snapshot.get("jobs", []):
            if job.get("status", {}).get("succeeded") or job.get("status", {}).get("failed"):
                continue
            spec = job["spec"]["template"]["spec"]
            if spec.get("nodeSelector", {}).get("kubernetes.io/hostname") == node["node"] and any(
                c.get("resources", {}).get("requests", {}).get(node["resource_key"])
                for c in spec.get("containers", [])
            ):
                return False
        if node["backend"] == "slurm" and any(
            r["nodes"] == node["node"] or r["requested_nodes"] == node["node"]
            for r in snapshot.get("slurm_rows", [])
        ):
            return False
    for workload in snapshot.get("workloads", []):
        conditions = workload.get("status", {}).get("conditions", [])
        if any(
            c["type"] == "QuotaReserved" and c["status"] == "True" for c in conditions
        ) and not any(c["type"] == "Finished" and c["status"] == "True" for c in conditions):
            return False
    keys = {n["resource_key"] for n in pool if n["backend"] == "kubernetes"}
    for queue in snapshot.get("clusterqueues", []):
        covered = {
            r
            for g in queue.get("spec", {}).get("resourceGroups", [])
            for r in g.get("coveredResources", [])
        }
        if covered & keys and (
            queue.get("status", {}).get("reservingWorkloads") != 0
            or queue.get("status", {}).get("pendingWorkloads") != 0
        ):
            return False
        if any(
            float(r["total"]) > 0
            for f in queue.get("status", {}).get("flavorsReservation", [])
            for r in f.get("resources", [])
            if r["name"] in keys
        ):
            return False
    return True


def native_snapshot(transport, directory, known, sequence):
    snapshot = transport.snapshot(known)
    path = directory / "snapshots" / f"{sequence:06d}.json"
    write(path, snapshot)
    snapshot.update(
        snapshot_ref=str(path.relative_to(directory)), snapshot_sha256=digest(path.read_bytes())
    )
    return snapshot


def wait_drained(transport, directory, known, pool, deadline, sequence):
    while time.monotonic() < deadline:
        snapshot = native_snapshot(transport, directory, known, sequence[0])
        sequence[0] += 1
        if pool_idle(snapshot, pool):
            return {k: snapshot[k] for k in ("snapshot_ref", "snapshot_sha256", "observed_at")}
        time.sleep(1)
    raise TimeoutError("native pool did not drain; no next arm submitted")


def manifest_for(directory, plan, node, item):
    path = (
        directory
        / "manifests"
        / (item["attempt_id"] + (".json" if node["backend"] == "kubernetes" else ".sbatch"))
    )
    if node["backend"] == "slurm":
        return path
    manifest = json.loads(archive_read(directory, node["template_file"]))
    manifest["metadata"]["name"] = item["attempt_id"]
    labels = {
        "resource-advisor/experiment": plan["experiment_id"],
        "resource-advisor/jct-run": plan["experiment_id"],
        "resource-advisor/jct-cohort": item["cohort_ref"],
    }
    manifest["metadata"].setdefault("labels", {}).update(labels)
    manifest["spec"].update(backoffLimit=0, activeDeadlineSeconds=plan["limits"]["per_job_seconds"])
    template = manifest["spec"]["template"]
    template.setdefault("metadata", {}).setdefault("labels", {}).update(labels)
    command = template["spec"]["containers"][0]["command"]
    command[command.index("--rounds") + 1] = str(plan["main_rounds"])
    if "--emit-compiled-artifact" in command:
        command.remove("--emit-compiled-artifact")
    for volume in template["spec"].get("volumes", []):
        if "configMap" in volume:
            volume["configMap"]["name"] = node.get("source_configmap", plan.get("source_configmap"))
    write(path, manifest)
    return path


def reservation_unit(node):
    if node["backend"] == "slurm":
        return "slurm_gpu_gres_seconds"
    if node["runtime"] == "openvino":
        return "kubernetes_npu_reservation_seconds"
    if node["resource_key"].endswith(".shared"):
        return "kubernetes_gpu_shared_slot_seconds"
    return "kubernetes_gpu_exclusive_seconds"


def export_results(directory, state):
    """Descriptive two-cohort metrics; retain distinct native reservation units."""
    nodes = {n["ref"]: n for n in state["plan"]["pool"]}
    rows = []
    arms = {}
    for cohort in state["cohorts"]:
        jobs = cohort["jobs"]
        complete = len(jobs) == state["plan"]["jobs_per_arm"] and all(
            j.get("outcome") == "SUCCEEDED" for j in jobs
        )
        costs = {}
        jcts = []
        for job in jobs:
            detail = job.get("details", {})
            unit = reservation_unit(nodes[job["candidate_ref"]])
            duration = job.get("gpu_seconds")
            if duration is not None:
                costs[unit] = costs.get(unit, 0) + duration
            jct = job["finished_at"] - job["accepted_at"] if "finished_at" in job else None
            if jct is not None:
                jcts.append(jct)
            rows.append(
                {
                    "arm": cohort["arm"],
                    "attempt_id": job["attempt_id"],
                    "candidate_ref": job["candidate_ref"],
                    "backend": job["backend"],
                    "runtime": job["runtime"],
                    "outcome": job.get("outcome", "INCOMPLETE"),
                    "native_id": job.get("native_id"),
                    "requested_forwards": job["requested_work"],
                    "images": detail.get("images"),
                    "quality": job.get("quality"),
                    "jct_seconds": jct,
                    "wait_seconds": job["started_at"] - job["accepted_at"]
                    if "started_at" in job
                    else None,
                    "synchronized_compute_seconds": detail.get("elapsed_seconds"),
                    "compilation_seconds": detail.get("compilation_seconds"),
                    "reservation_seconds": duration,
                    "reservation_unit": unit,
                    "submit_duration_seconds": job.get("submit_duration_seconds"),
                    "error": job.get("submit_error", job.get("evidence_error")),
                }
            )
        arms[cohort["arm"]] = {
            "jobs": len(jobs),
            "successful_jobs": sum(j.get("outcome") == "SUCCEEDED" for j in jobs),
            "complete": complete,
            "reservation_seconds": costs,
            "mean_jct_seconds": statistics.mean(jcts) if complete else None,
            "p95_jct_seconds": sorted(jcts)[math.ceil(len(jcts) * 0.95) - 1] if complete else None,
            "native_wall_seconds": cohort.get("wall_seconds"),
            "native_throughput_jobs_per_second": len(jobs) / cohort["wall_seconds"]
            if complete and cohort.get("wall_seconds")
            else None,
        }
    profile_costs = {}
    for ref, profile in state["frozen_profiles"].items():
        unit = reservation_unit(nodes[ref])
        profile_costs[unit] = profile_costs.get(unit, 0) + profile["observed_reservation_seconds"]
    summary = {
        "arms": arms,
        "profiling_reservation_seconds": profile_costs,
        "statistical_significance_established": False,
        "replication_unit": "one cohort per arm; descriptive only",
        "native_jct_boundary": "native acceptance to Kubernetes container exit / Slurm allocation EndTime",
        "profile_scope": "eight-forward compute scaled to full work plus observed startup/release; static cohort backlog",
        "physical_gpu_hours": None,
        "physical_energy_savings_established": False,
        "additional_failed_profile_cost": "preserved separately in plan.additional_failed_qualifications; charge in final combined report",
    }
    write(directory / "summary.json", summary)
    if rows:
        with (directory / "main.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
    outputs = {
        name: digest((directory / name).read_bytes())
        for name in ("capture.json", "summary.json", "main.csv")
        if (directory / name).is_file()
    }
    write(
        directory / "manifest.json",
        {
            "inputs": state["input_hashes"],
            "outputs": outputs,
            "runner_source_sha256": digest(Path(__file__).read_bytes()),
            "frozen_schedule_sha256": digest(
                (directory / "frozen-profiles-and-schedule.json").read_bytes()
            ),
            "native_preflight_sha256": digest((directory / "native-preflight.json").read_bytes()),
        },
    )


def run(directory, private_module=None, *, transport=None):
    """Archive choices/intents first; submit each native attempt exactly once."""
    directory = Path(directory).resolve()
    path = directory / "capture.json"
    if path.exists():
        raise ValueError("never replay an existing capture; inspect retained attempts")
    plan, qualifications, profiles, hashes = load_inputs(directory)
    orders = ["baseline", "reuse"]
    random.Random(plan["seed"]).shuffle(orders)
    assignments = {arm: select_assignments(plan, profiles, arm) for arm in orders}
    for folder in ("jobs", "logs", "manifests", "snapshots"):
        (directory / folder).mkdir(exist_ok=True)
    native = transport or ProfileTransport(import_private(private_module), directory, plan)
    write(directory / "native-preflight.json", native.preflight())
    (directory / "run_profile_all.py").write_bytes(Path(__file__).read_bytes())
    frozen = dict(
        at=time.time(),
        profiles=profiles,
        orders=orders,
        assignments=assignments,
        input_hashes=hashes,
        planned_arrival_offsets=[0.0] * len(plan["pool"]),
    )
    write(directory / "frozen-profiles-and-schedule.json", frozen)
    state = dict(
        plan=plan,
        qualifications=qualifications,
        frozen_profiles=profiles,
        orders=orders,
        cohorts=[],
        attempts=[],
        main_started_at=time.time(),
        input_hashes=hashes,
    )
    write(path, state)
    deadline = time.monotonic() + plan["limits"]["protocol_seconds"]
    nodes = {n["ref"]: n for n in plan["pool"]}
    sequence = [0]

    def save():
        for item in state["attempts"]:
            write(directory / "jobs" / (item["attempt_id"] + ".json"), item)
        write(path, state)

    try:
        for arm in orders:
            drained = wait_drained(
                native, directory, state["attempts"], plan["pool"], deadline, sequence
            )
            cohort = dict(
                arm=arm, block=0, jobs=[], drain_before=drained, client_started_at=time.time()
            )
            state["cohorts"].append(cohort)
            pending = []
            for index, ref in enumerate(assignments[arm]):
                item = dict(
                    attempt_id=f"{plan['experiment_id']}-{arm}-{index}",
                    candidate_ref=ref,
                    node_ref=nodes[ref]["node"],
                    backend=nodes[ref]["backend"],
                    runtime=nodes[ref]["runtime"],
                    requested_work=16384,
                    policy=arm,
                    cohort_ref=plan["experiment_id"] + "-" + arm,
                    planned_arrival_offset=0.0,
                    intent_created_at=time.time(),
                    profile_source=profiles[ref]["source_attempt"] if arm == "reuse" else None,
                )
                manifest = manifest_for(directory, plan, nodes[ref], item)
                cohort["jobs"].append(item)
                state["attempts"].append(item)
                pending.append((item, nodes[ref], manifest))
                with (directory / "intent-ledger.jsonl").open("a") as ledger:
                    ledger.write(
                        json.dumps(
                            dict(
                                attempt_id=item["attempt_id"],
                                intent_created_at=item["intent_created_at"],
                            )
                        )
                        + "\n"
                    )
                    ledger.flush()
                    os.fsync(ledger.fileno())
            save()

            def submit(item, node, manifest):
                local = copy.deepcopy(item)
                local["request_started_at"] = time.time()
                began = time.monotonic()
                try:
                    native.submit(local, node, manifest)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                    local["submit_error"] = str(exc)
                local["submit_response_at"] = time.time()
                local["submit_duration_seconds"] = time.monotonic() - began
                return local

            with ThreadPoolExecutor(max_workers=len(pending)) as executor:
                futures = [
                    (item, executor.submit(submit, item, node, manifest))
                    for item, node, manifest in pending
                ]
                for item, future in futures:
                    item.update(future.result(timeout=55))
                    save()
            if any(j.get("submit_error") for j in cohort["jobs"]):
                raise ValueError("uncertain native acceptance retained; never retry")
            arm_deadline = min(deadline, time.monotonic() + 600)
            while time.monotonic() < arm_deadline:
                snapshot = native_snapshot(native, directory, state["attempts"], sequence[0])
                sequence[0] += 1
                for item in cohort["jobs"]:
                    if item.get("outcome"):
                        continue
                    try:
                        receipt = native.receipt(item, nodes[item["candidate_ref"]], snapshot)
                        if receipt is None:
                            continue
                        apply_receipt(item, receipt)
                        if item["outcome"] == "SUCCEEDED":
                            result = receipt["result"]
                            source = profiles[item["candidate_ref"]]
                            validate_runtime_result(
                                result, plan, nodes[item["candidate_ref"]], 16384
                            )
                            if result["runtime_versions"] != source["runtime_versions"] or any(
                                result.get(k) != source[k] for k in ("model_sha256", "input_sha256")
                            ):
                                raise ValueError(
                                    "main runtime/model/input differs from frozen qualification"
                                )
                        item["observed_complete_at"] = time.time()
                    except (ValueError, KeyError, TypeError) as exc:
                        item.update(outcome="FAILED", evidence_error=str(exc))
                    save()
                if any(j.get("outcome") == "FAILED" for j in cohort["jobs"]):
                    raise ValueError("failed native main evidence retained")
                if all(j.get("outcome") == "SUCCEEDED" for j in cohort["jobs"]):
                    break
                time.sleep(1)
            if not all(j.get("outcome") == "SUCCEEDED" for j in cohort["jobs"]):
                raise TimeoutError("bounded main cohort incomplete; preserve native attempts")
            cohort.update(
                started_at=min(j["accepted_at"] for j in cohort["jobs"]),
                wall_seconds=max(j["finished_at"] for j in cohort["jobs"])
                - min(j["accepted_at"] for j in cohort["jobs"]),
            )
            cohort["drain_after"] = wait_drained(
                native, directory, state["attempts"], plan["pool"], deadline, sequence
            )
            save()
        state["completed_at"] = time.time()
        save()
        export_results(directory, state)
        return state
    except BaseException:
        state["abort"] = native.stop_owned(state["attempts"])
        save()
        export_results(directory, state)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--private-remote-module", type=Path)
    parser.add_argument(
        "--preflight", action="store_true", help="read-only native source/capacity verification"
    )
    args = parser.parse_args()
    if args.preflight:
        plan = validate_plan(json.loads((args.directory / "plan.json").read_text()))
        private = import_private(args.private_remote_module) if args.private_remote_module else None
        print(json.dumps(ProfileTransport(private, args.directory, plan).preflight()))
    else:
        state = run(args.directory, args.private_remote_module)
        print(
            json.dumps(dict(completed_at=state["completed_at"], native_jobs=len(state["attempts"])))
        )


if __name__ == "__main__":
    main()
