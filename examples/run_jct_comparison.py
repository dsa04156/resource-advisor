"""Bounded experimental three-policy native comparison; no production integration.

Uses an already completed capacity+1 calibration and its immutable source on both
backends. Never creates a new profile or retries an uncertain native submission.
The private module must expose ``remote(role, python_code, timeout=...)``.
"""

import argparse
import copy
import importlib.util
import json
import os
import shlex
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from jct_native import (
    TERMINAL,
    build_queues,
    digest,
    freeze_profiles,
    normalize_kubernetes,
    normalize_slurm,
    parse_sacct,
    parse_squeue,
)

from resource_advisor.jct_selection import (
    PROFILE_QUEUE,
    ROUND_ROBIN,
    V2_PROFILE_ONLY,
    SubmitIntent,
    select_candidate,
)

NAMESPACE = "resource-advisor-lab"
TIME_ENV = {"TZ": "UTC", "SLURM_TIME_FORMAT": "%Y-%m-%dT%H:%M:%S%z"}


def write(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def file_ref(directory, path):
    return {"job_file": str(path.relative_to(directory)), "job_sha256": digest(path.read_bytes())}


def import_private(path):
    spec = importlib.util.spec_from_file_location("jct_private_remote", path)
    if spec is None or spec.loader is None:
        raise ValueError("private remote module unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NativeTransport:
    """Native CLI system boundary; credentials remain in the injected module."""

    def __init__(self, private, directory, plan):
        self.private = private
        self.directory = directory
        self.plan = plan
        self.remote_directory = "/tmp/ra-" + plan["experiment_id"]

    def kube(self, *args, structured=True):
        output = subprocess.check_output(
            ["rtk", "proxy", "kubectl", "--request-timeout=15s", "-n", NAMESPACE, *args],
            text=True,
            timeout=25,
        )
        return json.loads(output) if structured else output

    def command(self, role, args, *, input_text=None):
        code = (
            "import subprocess,json,os; p=subprocess.run("
            + repr(args)
            + ",input="
            + repr(input_text)
            + ",capture_output=True,text=True,timeout=20,env=dict(os.environ,**"
            + repr(TIME_ENV)
            + "));print(json.dumps(dict(exit=p.returncode,stdout=p.stdout,stderr=p.stderr)))"
        )
        value = json.loads(self.private.remote(role, code, timeout=30))
        if value["exit"]:
            raise RuntimeError(
                "native command failed: " + str(args[0]) + ": " + value["stderr"][:300]
            )
        return value

    def preflight(self):
        source = self.kube("get", "configmap", self.plan["experiment_id"] + "-source", "-o", "json")
        for name, expected in self.plan["transfer_checksums"].items():
            if digest(source["data"][name].encode()) != expected:
                raise ValueError("existing native source changed")
        code = (
            "import pathlib,hashlib,json; print(json.dumps({n:hashlib.sha256((pathlib.Path("
            + repr(self.remote_directory)
            + ")/n).read_bytes()).hexdigest() for n in "
            + repr(list(self.plan["transfer_checksums"]))
            + "}))"
        )
        transferred = json.loads(self.private.remote("slurm_orin", code, timeout=20))
        if transferred != self.plan["transfer_checksums"]:
            raise ValueError("Slurm worker source changed")
        nodes = self.kube("get", "nodes", "-o", "json")
        node_map = {node["metadata"]["name"]: node for node in nodes["items"]}
        for node in self.plan["pool"]:
            if (
                node["backend"] == "kubernetes"
                and int(
                    node_map[node["node"]]["status"]["allocatable"].get(node["resource_key"], 0)
                )
                != node["nominal_slots"]
            ):
                raise ValueError("native capacity changed")
        return dict(
            at=time.time(),
            source_metadata=source["metadata"],
            transfer_checksums=transferred,
            nodes=nodes,
        )

    def snapshot(self, known):
        start = time.time()
        mono = time.monotonic()
        current_cohort = known[-1].get("cohort_ref") if known else None
        current_known = [
            i for i in known if not current_cohort or i.get("cohort_ref") == current_cohort
        ]
        ids = sorted(
            {
                str(i["native_id"])
                for i in current_known
                if i.get("native_id") and i["backend"] == "slurm"
            }
        )
        selector = (
            "resource-advisor/jct-cohort=" + current_cohort
            if current_cohort
            else "resource-advisor/experiment=" + self.plan["experiment_id"]
        )

        def batch_kube():
            return self.kube(
                "get", "jobs,pods,workloads.kueue.x-k8s.io", "-l", selector, "-o", "json"
            )

        def all_pods():
            return self.kube(
                "get",
                "pods,clusterqueues.kueue.x-k8s.io",
                "--all-namespaces",
                "-l",
                "resource-advisor/experiment!=" + self.plan["experiment_id"],
                "-o",
                "json",
            )

        def batch_slurm():
            requests = {
                "queue": [
                    "squeue",
                    "-h",
                    "-w",
                    "slurm-w2",
                    "--format=%i|%100j|%a|%P|%q|%T|%N|%n|%30S|%30V|%r",
                ]
            }
            if ids:
                requests["accounting"] = [
                    "sacct",
                    "-nP",
                    "--duplicates",
                    "-S",
                    self.plan["slurm_accounting_start"],
                    "-j",
                    ",".join(ids),
                    "--format=JobIDRaw,JobName%100,State,ExitCode,Submit%30,Start%30,End%30,ElapsedRaw,ReqTRES%200,AllocTRES%200,NodeList,Account,QOS",
                ]
            code = (
                "import subprocess,json,os; requests="
                + repr(requests)
                + "; result={};\nfor name,args in requests.items():\n p=subprocess.run(args,capture_output=True,text=True,timeout=20,env=dict(os.environ,**"
                + repr(TIME_ENV)
                + "));result[name]=dict(exit=p.returncode,stdout=p.stdout,stderr=p.stderr)\nprint(json.dumps(result))"
            )
            return json.loads(self.private.remote("controller", code, timeout=45))

        results, requests, errors = {}, {}, []

        def measured(name, operation):
            began, began_mono = time.time(), time.monotonic()
            try:
                result = operation()
                error = None
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                result, error = None, str(exc)
            return (
                name,
                result,
                dict(
                    started_at=began,
                    finished_at=time.time(),
                    duration_seconds=time.monotonic() - began_mono,
                    started_monotonic=began_mono,
                    finished_monotonic=time.monotonic(),
                    error=error,
                ),
            )

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(measured, name, operation)
                for name, operation in (
                    ("kubernetes", batch_kube),
                    ("all_pods", all_pods),
                    ("slurm", batch_slurm),
                )
            ]
            for future in futures:
                name, result, request = future.result()
                requests[name] = request
                results[name] = result
                if request["error"]:
                    errors.append(dict(backend=name, error=request["error"]))
        items = (results["kubernetes"] or {}).get("items", [])
        snapshot = dict(
            observed_at=start,
            finished_at=time.time(),
            duration_seconds=time.monotonic() - mono,
            requests=requests,
            raw=results,
            errors=errors,
            jobs=[i for i in items if i["kind"] == "Job"],
            pods=[i for i in items if i["kind"] == "Pod"],
            workloads=[i for i in items if i["kind"] == "Workload"],
            all_pods=[
                i for i in (results["all_pods"] or {}).get("items", []) if i["kind"] == "Pod"
            ],
            clusterqueues=[
                i
                for i in (results["all_pods"] or {}).get("items", [])
                if i["kind"] == "ClusterQueue"
            ],
            slurm_rows=[],
            accounting_rows=[],
            own_slurm_ids=ids,
            current_cohort=current_cohort,
            namespace_selector=selector,
            scope="current cohort Jobs/Pods/Kueue Workloads + nonexperiment cluster Pods and all ClusterQueues; node-scoped Slurm squeue, current exact owned-ID sacct; prior cohorts terminal and drained",
        )
        if results["slurm"]:
            for key, parser, field in (
                ("queue", parse_squeue, "slurm_rows"),
                ("accounting", parse_sacct, "accounting_rows"),
            ):
                result = results["slurm"].get(key)
                if not result:
                    continue
                if result["exit"]:
                    snapshot["errors"].append(
                        dict(backend="slurm", command=key, error=result["stderr"])
                    )
                else:
                    try:
                        snapshot[field] = parser(result["stdout"])
                    except ValueError as exc:
                        snapshot["errors"].append(
                            dict(backend="slurm", command=key, error=str(exc))
                        )
        return snapshot

    def submit(self, item, node, manifest):
        item["request_started_at"] = time.time()
        item["request_started_monotonic"] = time.monotonic()
        if node["backend"] == "kubernetes":
            value = self.kube("create", "-f", str(manifest), "-o", "json")
            item["native_id"] = value["metadata"]["name"]
            item["native_uid"] = value["metadata"]["uid"]
            item["submission"] = value
        else:
            script = "#!/bin/bash\nset -euo pipefail\n"
            for filename, checksum in self.plan["transfer_checksums"].items():
                script += (
                    "printf '%s  %s\\n' "
                    + shlex.quote(checksum)
                    + " "
                    + shlex.quote(self.remote_directory + "/" + filename)
                    + " | sha256sum --check --status\n"
                )
            script += (
                "exec srun --export=ALL python3 -I -u "
                + shlex.quote(self.remote_directory + "/workload.py")
                + " --fixture "
                + shlex.quote(self.remote_directory + "/fixture.json")
                + " --rounds "
                + str(item["requested_work"])
                + "\n"
            )
            manifest.write_text(script)
            args = [
                "sbatch",
                "--parsable",
                "--job-name=" + item["attempt_id"],
                "--account=ra-lab",
                "--qos=ra-normal",
                "--partition=compute",
                "--nodelist=slurm-w2",
                "--nodes=1",
                "--ntasks=1",
                "--cpus-per-task=1",
                "--mem=512M",
                "--gres=gpu:orin_nano:1",
                "--time=00:02:00",
                "--no-requeue",
                "--output=" + self.remote_directory + "/" + item["attempt_id"] + ".log",
            ]
            value = self.command("controller", args, input_text=script)
            item["submission"] = value
            native_id = value["stdout"].strip().split(";")[0]
            if not native_id.isdigit():
                raise ValueError("uncertain Slurm acceptance; do not resubmit")
            item["native_id"] = native_id
        item["submit_response_at"] = time.time()
        item["submit_duration_seconds"] = time.monotonic() - item["request_started_monotonic"]
        return item

    def receipt(self, item, node, snapshot):
        if node["backend"] == "kubernetes":
            jobs = [j for j in snapshot["jobs"] if j["metadata"]["name"] == item["attempt_id"]]
            if not jobs:
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
            if not pods or not any(
                s.get("state", {}).get("terminated")
                for p in pods
                for s in p.get("status", {}).get("containerStatuses", [])
            ):
                return None
            log = self.kube("logs", "job/" + item["attempt_id"], structured=False)
            observed = normalize_kubernetes(
                job, pods, item, node, self.plan["experiment_id"], log, self.plan
            )
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
            observed = normalize_slurm(rows, item, log, self.plan)
        (self.directory / "logs" / (item["attempt_id"] + ".log")).write_text(log)
        observed["log_file"] = "logs/" + item["attempt_id"] + ".log"
        observed["native_snapshot_ref"] = snapshot["snapshot_ref"]
        observed["native_snapshot_sha256"] = snapshot["snapshot_sha256"]
        return observed


class Observer:
    """One fixed cadence across all policies; only the queue arm consumes state."""

    def __init__(self, transport, directory, known, lock, cadence=2.0):
        self.transport, self.directory, self.known, self.lock = transport, directory, known, lock
        self.cadence = cadence
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.latest = None
        self.sequence = 0
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stop.is_set():
            started = time.monotonic()
            with self.lock:
                known = copy.deepcopy(self.known)
            snapshot = self.transport.snapshot(known)
            path = self.directory / "snapshots" / f"{self.sequence:06d}.json"
            write(path, snapshot)
            snapshot["snapshot_ref"] = str(path.relative_to(self.directory))
            snapshot["snapshot_sha256"] = digest(path.read_bytes())
            with self.lock:
                self.latest = snapshot
            self.sequence += 1
            self.ready.set()
            self.stop.wait(max(0.0, self.cadence - (time.monotonic() - started)))

    def current(self):
        with self.lock:
            return copy.deepcopy(self.latest)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=55)


def manifest_for(directory, v2_directory, node, item, plan):
    suffix = ".json" if node["backend"] == "kubernetes" else ".sbatch"
    path = directory / "manifests" / (item["attempt_id"] + suffix)
    if node["backend"] == "kubernetes":
        template = (
            "f0-spark-runtime.json" if node["ref"] == "spark" else "f0-" + node["ref"] + ".json"
        )
        manifest = json.loads((v2_directory / template).read_text())
        manifest["metadata"]["name"] = item["attempt_id"]
        manifest["metadata"]["labels"]["resource-advisor/experiment"] = plan["experiment_id"]
        manifest["metadata"]["labels"]["resource-advisor/jct-cohort"] = item["cohort_ref"]
        manifest["spec"]["activeDeadlineSeconds"] = 120
        pod = manifest["spec"]["template"]
        pod["metadata"]["labels"]["resource-advisor/experiment"] = plan["experiment_id"]
        pod["metadata"]["labels"]["resource-advisor/jct-cohort"] = item["cohort_ref"]
        pod["spec"]["containers"][0]["command"][-1] = str(plan["main_rounds"])
        pod["spec"]["volumes"][0]["configMap"]["name"] = plan["experiment_id"] + "-source"
        write(path, manifest)
    return path


def save_job(directory, item):
    path = directory / "jobs" / (item["attempt_id"] + ".json")
    write(path, item)
    return file_ref(directory, path)


def apply_receipt(item, receipt):
    item["native_receipt"] = receipt
    item.update(
        accepted_at=receipt["accepted_at"],
        started_at=receipt["scheduled_at"],
        finished_at=receipt["finished_at"],
        gpu_seconds=receipt["gpu_seconds"],
        outcome="SUCCEEDED" if receipt["termination"]["exitCode"] == 0 else "FAILED",
    )
    result = receipt["result"]
    if result is not None:
        item.update(
            units=result["images"],
            quality=result["quality"],
            details=dict(
                model_digest=result["fixture_sha256"],
                input_digest=result["fixture_sha256"],
                **result,
            ),
            sensor=[
                dict(at=s["at"], utilization_percent=s["utilization"]) for s in result["sensor"]
            ],
        )


def drain(observer, known, identities, profiles, pool, experiment, *, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = observer.current()
        if snapshot is not None:
            queues = build_queues(snapshot, identities, profiles, pool, known, experiment)
            cq = [
                q
                for q in snapshot.get("clusterqueues", [])
                if q["metadata"]["name"] == "hairp-gpu-pool"
            ]
            quota_free = (
                len(cq) == 1
                and cq[0].get("status", {}).get("reservingWorkloads") == 0
                and cq[0].get("status", {}).get("pendingWorkloads") == 0
                and all(
                    r["total"] == "0"
                    for f in cq[0].get("status", {}).get("flavorsReservation", [])
                    for r in f.get("resources", [])
                    if r["name"].startswith("nvidia.com/")
                )
            )
            if (
                time.time() - snapshot["observed_at"] <= 5.0
                and quota_free
                and all(q.complete and not q.running and not q.pending for q in queues)
            ):
                invisible = [
                    i
                    for i in known
                    if not i.get("outcome")
                    and i["attempt_id"]
                    not in set().union(*(set(q.observed_job_ids) for q in queues))
                ]
                if not invisible:
                    return {
                        "snapshot_ref": snapshot["snapshot_ref"],
                        "snapshot_sha256": snapshot["snapshot_sha256"],
                        "observed_at": snapshot["observed_at"],
                        "drained_at": time.time(),
                    }
        time.sleep(0.5)
    raise TimeoutError("native pool did not drain; no new cohort submitted")


def stop_owned(transport, directory, known, plan):
    errors = []
    for item in known:
        if item.get("outcome"):
            continue
        try:
            if item["backend"] == "kubernetes":
                job = transport.kube("get", "job", item["attempt_id"], "-o", "json")
                if (
                    job["metadata"].get("labels", {}).get("resource-advisor/experiment")
                    != plan["experiment_id"]
                ):
                    raise ValueError("abort refused: foreign Job")
                item["partial_native_evidence"] = {
                    "job": job,
                    "pods": transport.kube(
                        "get", "pods", "-l", "job-name=" + item["attempt_id"], "-o", "json"
                    ),
                }
                save_job(directory, item)
                transport.kube(
                    "patch",
                    "job",
                    item["attempt_id"],
                    "--type=merge",
                    "-p",
                    json.dumps(
                        {
                            "metadata": {"resourceVersion": job["metadata"]["resourceVersion"]},
                            "spec": {"suspend": True},
                        }
                    ),
                    structured=False,
                )
            elif item.get("native_id"):
                value = transport.command(
                    "controller",
                    [
                        "squeue",
                        "-h",
                        "-j",
                        item["native_id"],
                        "--format=%i|%100j|%a|%P|%q|%T|%N|%n|%30S|%30V|%r",
                    ],
                )
                rows = parse_squeue(value["stdout"])
                if (
                    len(rows) == 1
                    and rows[0]["native_id"] == item["native_id"]
                    and rows[0]["name"] == item["attempt_id"]
                    and rows[0]["account"] == "ra-lab"
                    and rows[0]["qos"] == "ra-normal"
                ):
                    item["partial_native_evidence"] = rows[0]
                    save_job(directory, item)
                    transport.command("controller", ["scancel", item["native_id"]])
                else:
                    raise ValueError("abort uncertain identity: retained, not cancelled")
            else:
                raise ValueError(
                    "uncertain acceptance: retained intent; no name-based cancellation"
                )
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            errors.append(dict(attempt_id=item["attempt_id"], error=str(exc)))
    return dict(at=time.time(), errors=errors)


def run(directory, v2_directory, private_module, *, observer_cadence=2.0):
    path = directory / "capture.json"
    calibration = json.loads(path.read_text())
    if calibration.get("cohorts") or calibration.get("main_started_at"):
        raise ValueError("never replay an existing main capture")
    identities, profiles, evidence = freeze_profiles(calibration)
    schedule_path = directory / "preregistered-schedule.json"
    schedule = json.loads(schedule_path.read_text())
    if len(schedule["cohorts"]) != 27 or schedule["seed"] != 20261008:
        raise ValueError("invalid preregistered schedule")
    expected = {
        (load, block, arm)
        for load in ("sparse", "moderate", "burst")
        for block in range(3)
        for arm in (ROUND_ROBIN, V2_PROFILE_ONLY, PROFILE_QUEUE)
    }
    if {(c["load"], c["block"], c["arm"]) for c in schedule["cohorts"]} != expected or any(
        c["jobs"] != 12 for c in schedule["cohorts"]
    ):
        raise ValueError("preregistered schedule changed")
    layouts = []
    for position, row in enumerate(schedule["cohorts"]):
        layouts.append(
            dict(
                load=row["load"],
                block=row["block"],
                arm=row["arm"],
                interval_seconds=row["arrival_interval_seconds"],
                order_in_block=position % 3,
            )
        )
    if (directory / "calibration.json").exists():
        raise ValueError("calibration archival exists; inspect instead of replaying")
    write(directory / "calibration.json", calibration)
    for folder in ("jobs", "logs", "manifests", "choices", "snapshots", "profiles"):
        (directory / folder).mkdir(exist_ok=True)
    plan = copy.deepcopy(calibration["plan"])
    plan.update(
        transfer_checksums=calibration["transfer_checksums"],
        jobs_per_cohort=12,
        paired_blocks=3,
        main_rounds=16384,
        slurm_timezone="UTC",
        slurm_accounting_start=datetime.fromtimestamp(
            min(j["accepted_at"] for j in calibration["qualifications"]) - 60, timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%S"),
        baseline_policy="Round Robin in frozen physical candidate order",
        profile_policy="same fresh full-work frozen native service estimate + unchanged v2 synthetic cohort backlog; no live reconciliation",
        profiling_source_rounds=16384,
        replication="three paired cohort repeats per load; cohorts are replicate units",
        randomization="seeded per-load permutation, rotated across three paired repeats",
        observer_cadence_seconds=observer_cadence,
        limits=dict(per_job_seconds=120, max_main_jobs=324, protocol_seconds=2700),
        schedule=layouts,
        schedule_file="preregistered-schedule.json",
        schedule_sha256=digest(schedule_path.read_bytes()),
    )
    plan.pop("user_shortening", None)
    plan.pop("slurm_status", None)
    plan.pop("profile_independent_jobs_per_node", None)
    plan["orders"] = [cohort["arm"] for cohort in plan["schedule"]]
    for ref, row in evidence.items():
        write(directory / "profiles" / (ref + ".json"), row)
    import resource_advisor.jct_selection as policy_module

    policy_source = Path(policy_module.__file__).read_bytes()
    (directory / "jct_selection.py").write_bytes(policy_source)
    plan["policy_sha256"] = digest(policy_source)
    state = dict(
        plan=plan,
        qualifications=calibration["qualifications"],
        profiling=calibration.get("profiling"),
        calibration_source=dict(
            file="calibration.json", sha256=digest((directory / "calibration.json").read_bytes())
        ),
        profiles_frozen_at=time.time(),
        frozen_profiles=[asdict(p) for p in profiles],
        cohorts=[],
        attempts=[],
        pool_sensor_qualified=False,
        main_started_at=time.time(),
    )
    write(directory / "plan.json", plan)
    write(path, state)
    transport = NativeTransport(import_private(private_module), directory, plan)
    write(directory / "native-preflight.json", transport.preflight())
    known, lock = [], threading.Lock()
    observer = Observer(transport, directory, known, lock, observer_cadence)
    observer.thread.start()
    protocol_deadline = time.monotonic() + plan["limits"]["protocol_seconds"]
    nodes = {n["ref"]: n for n in plan["pool"]}
    profile_map = {p.identity.candidate_ref: p for p in profiles}
    try:
        if not observer.ready.wait(timeout=55):
            raise TimeoutError("native observer unavailable")
        with ThreadPoolExecutor(max_workers=12) as executor:
            for layout in plan["schedule"]:
                if time.monotonic() > protocol_deadline:
                    raise TimeoutError("bounded protocol deadline exceeded")
                cohort = dict(
                    layout,
                    jobs=[],
                    drain_before=drain(
                        observer, known, identities, profiles, plan["pool"], plan["experiment_id"]
                    ),
                    client_started_at=time.time(),
                )
                state["cohorts"].append(cohort)
                write(path, state)
                start = time.monotonic()
                backlog = {n["ref"]: 0.0 for n in plan["pool"]}
                futures, jobs = [], []
                for index in range(12):
                    planned = start + layout["interval_seconds"] * index
                    delay = planned - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                    decision_started = time.time()
                    snapshot = observer.current()
                    with lock:
                        current = copy.deepcopy(known)
                    queues = (
                        build_queues(
                            snapshot,
                            identities,
                            profiles,
                            plan["pool"],
                            current,
                            plan["experiment_id"],
                        )
                        if layout["arm"] == PROFILE_QUEUE
                        else ()
                    )
                    intents = (
                        tuple(
                            SubmitIntent(
                                next(
                                    c
                                    for c in identities
                                    if c.candidate_ref == item["candidate_ref"]
                                ),
                                item["attempt_id"],
                                item["intent_created_at"],
                                profile_map[item["candidate_ref"]].compute_seconds
                                + profile_map[item["candidate_ref"]].preparation_seconds
                                + profile_map[item["candidate_ref"]].release_seconds,
                                item.get(
                                    "expected_admission_seconds",
                                    profile_map[item["candidate_ref"]].readmission_seconds,
                                ),
                            )
                            for item in current
                            if not item.get("outcome")
                        )
                        if layout["arm"] == PROFILE_QUEUE
                        else ()
                    )
                    selection_at = time.time()
                    selection_backlog = copy.deepcopy(backlog)
                    decision = select_candidate(
                        layout["arm"],
                        identities,
                        profiles,
                        now_seconds=selection_at,
                        queues=queues,
                        intents=intents,
                        rr_index=index,
                        cohort_backlog_seconds=backlog,
                    )
                    fallback = None
                    selected = decision.candidate_ref
                    if selected is None:
                        fallback = asdict(
                            select_candidate(
                                ROUND_ROBIN,
                                identities,
                                profiles,
                                now_seconds=selection_at,
                                rr_index=index,
                            )
                        )
                        selected = fallback["candidate_ref"]
                    profile = profile_map[selected]
                    admission_estimate = (
                        next(
                            (
                                p.admission_seconds
                                for p in decision.predictions
                                if p.candidate_ref == selected
                            ),
                            None,
                        )
                        if layout["arm"] == PROFILE_QUEUE
                        else profile.readmission_seconds
                    )
                    if layout["arm"] == V2_PROFILE_ONLY:
                        backlog[selected] += profile.v2_service_seconds
                    name = (
                        plan["experiment_id"]
                        + "-"
                        + layout["load"][0]
                        + str(layout["block"])
                        + "-"
                        + {ROUND_ROBIN: "rr", V2_PROFILE_ONLY: "po", PROFILE_QUEUE: "pq"}[
                            layout["arm"]
                        ]
                        + "-"
                        + str(index)
                    )
                    choice = dict(
                        policy=layout["arm"],
                        candidate_ref=selected,
                        selection_at=selection_at,
                        rr_index=index,
                        max_queue_age_seconds=5.0,
                        max_profile_age_seconds=86400.0,
                        decision_started_at=decision_started,
                        decision_finished_at=time.time(),
                        decision=asdict(decision),
                        fallback=fallback,
                        profile_source_refs=[
                            phase["attempt_id"] for phase in evidence[selected]["raw_phases"]
                        ]
                        if layout["arm"] != ROUND_ROBIN
                        else [],
                        profiles=[asdict(p) for p in profiles]
                        if layout["arm"] != ROUND_ROBIN
                        else [],
                        queues=[asdict(q) for q in queues],
                        intents=[asdict(i) for i in intents],
                        snapshot_ref=snapshot["snapshot_ref"],
                        snapshot_sha256=snapshot["snapshot_sha256"],
                        snapshot_age_seconds=time.time() - snapshot["observed_at"],
                        cohort_backlog_seconds=selection_backlog
                        if layout["arm"] == V2_PROFILE_ONLY
                        else None,
                    )
                    choice_path = directory / "choices" / (name + ".json")
                    write(choice_path, choice)
                    item = dict(
                        attempt_id=name,
                        expected_admission_seconds=admission_estimate,
                        cohort_ref=layout["load"]
                        + "-"
                        + str(layout["block"])
                        + "-"
                        + layout["arm"],
                        profile_source=choice["profile_source_refs"][0]
                        if choice["profile_source_refs"]
                        else None,
                        candidate_ref=selected,
                        node_ref=nodes[selected]["node"],
                        backend=nodes[selected]["backend"],
                        requested_work=16384,
                        policy=layout["arm"],
                        planned_arrival_offset=layout["interval_seconds"] * index,
                        planned_arrival_monotonic=planned,
                        decision_lateness_seconds=time.monotonic() - planned,
                        intent_created_at=time.time(),
                        choice_evidence={
                            "choice_file": str(choice_path.relative_to(directory)),
                            "choice_sha256": digest(choice_path.read_bytes()),
                        },
                    )
                    manifest = manifest_for(directory, v2_directory, nodes[selected], item, plan)
                    with lock:
                        known.append(item)
                    jobs.append(item)
                    cohort["jobs"].append(save_job(directory, item))
                    state["attempts"].append(
                        dict(attempt_id=name, candidate_ref=selected, backend=item["backend"])
                    )
                    with (directory / "intent-ledger.jsonl").open("a") as ledger:
                        ledger.write(
                            json.dumps(
                                dict(
                                    attempt_id=name,
                                    job_file=cohort["jobs"][-1]["job_file"],
                                    intent_created_at=item["intent_created_at"],
                                )
                            )
                            + "\n"
                        )
                        ledger.flush()
                        os.fsync(ledger.fileno())

                    def submit(item=item, node=nodes[selected], manifest=manifest):
                        local = copy.deepcopy(item)
                        try:
                            transport.submit(local, node, manifest)
                        except (
                            OSError,
                            ValueError,
                            RuntimeError,
                            subprocess.SubprocessError,
                        ) as exc:
                            local["submit_error"] = str(exc)
                            local["submit_response_at"] = time.time()
                        with lock:
                            item.update(local)
                        save_job(directory, item)

                    futures.append(executor.submit(submit))
                for future in futures:
                    future.result(timeout=55)
                if any(j.get("submit_error") for j in jobs):
                    raise ValueError("native submission uncertainty retained; no retries")
                deadline = min(protocol_deadline, time.monotonic() + 180)
                while time.monotonic() < deadline:
                    snapshot = observer.current()
                    for item in jobs:
                        if item.get("outcome"):
                            continue
                        observed = transport.receipt(item, nodes[item["candidate_ref"]], snapshot)
                        if observed is None:
                            continue
                        with lock:
                            apply_receipt(item, observed)
                        item["observed_complete_at"] = time.time()
                        item["actual_arrival_offset"] = (
                            item["accepted_at"] - cohort["client_started_at"]
                        )
                        item["native_acceptance_lateness_seconds"] = item["accepted_at"] - (
                            cohort["client_started_at"] + item["planned_arrival_offset"]
                        )
                        save_job(directory, item)
                        print(
                            json.dumps(
                                dict(
                                    phase="native_complete",
                                    arm=layout["arm"],
                                    load=layout["load"],
                                    block=layout["block"],
                                    candidate=item["candidate_ref"],
                                    outcome=item["outcome"],
                                    jct=item["finished_at"] - item["accepted_at"],
                                )
                            ),
                            flush=True,
                        )
                    if all(j.get("outcome") for j in jobs):
                        break
                    time.sleep(0.5)
                cohort["jobs"] = [save_job(directory, j) for j in jobs]
                write(path, state)
                if not all(j.get("outcome") == "SUCCEEDED" for j in jobs):
                    raise ValueError("cohort failed/incomplete; attempts preserved")
                cohort.update(
                    started_at=min(j["accepted_at"] for j in jobs),
                    wall_seconds=max(j["finished_at"] for j in jobs)
                    - min(j["accepted_at"] for j in jobs),
                    client_wall_seconds=time.time() - cohort["client_started_at"],
                    actual_acceptance_spread_seconds=max(j["accepted_at"] for j in jobs)
                    - min(j["accepted_at"] for j in jobs),
                    completed_at=time.time(),
                )
                cohort["drain_after"] = drain(
                    observer, known, identities, profiles, plan["pool"], plan["experiment_id"]
                )
                write(path, state)
                print(
                    json.dumps(
                        dict(
                            phase="cohort_complete",
                            arm=layout["arm"],
                            load=layout["load"],
                            block=layout["block"],
                            wall_seconds=cohort["wall_seconds"],
                        )
                    ),
                    flush=True,
                )
        state["completed_at"] = time.time()
        state["observer_snapshots"] = observer.sequence
        write(path, state)
    except BaseException:
        observer.close()
        state["abort"] = stop_owned(transport, directory, known, plan)
        for cohort in state["cohorts"]:
            cohort["jobs"] = [
                save_job(directory, j)
                for j in known
                if j["attempt_id"].startswith(
                    plan["experiment_id"]
                    + "-"
                    + cohort["load"][0]
                    + str(cohort["block"])
                    + "-"
                    + {ROUND_ROBIN: "rr", V2_PROFILE_ONLY: "po", PROFILE_QUEUE: "pq"}[cohort["arm"]]
                    + "-"
                )
            ]
        write(path, state)
        raise
    finally:
        observer.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--v2-directory", type=Path, required=True)
    parser.add_argument("--private-remote-module", type=Path, required=True)
    args = parser.parse_args()
    run(args.directory.resolve(), args.v2_directory.resolve(), args.private_remote_module.resolve())
