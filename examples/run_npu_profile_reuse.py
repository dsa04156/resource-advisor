"""Bounded native comparisons of already qualified, distinct NPU workloads.

These are separate workload groups, never a GPU/NPU same-model ranking.
Singleton Mobilint/Rockchip groups are manual observe executions; incomplete
firmware identity remains a production recommendation/reuse blocker.
"""

import argparse
import copy
import hashlib
import json
import math
import random
import shutil
import statistics
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from resource_advisor.contracts import Candidate, RuntimeVariant, signature
from resource_advisor.hailo_qualification import quality_gates
from resource_advisor.policy import context_signature

SEED = 20261008
NAMESPACE = "resource-advisor-lab"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def choose_candidate(arm, candidates, profiles, backlog, index):
    if arm == "round_robin":
        return candidates[index % len(candidates)]
    if arm != "profile_reuse" or set(profiles) != set(candidates):
        raise ValueError("complete frozen profiles and a known arm required")
    return min(
        candidates, key=lambda ref: (backlog.get(ref, 0) + profiles[ref]["service_seconds"], ref)
    )


def epoch(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed.timestamp()


def native_timing(job, pods, candidate):
    if len(pods) != 1 or not any(
        o.get("kind") == "Job" and o.get("uid") == job["metadata"]["uid"]
        for o in pods[0]["metadata"].get("ownerReferences", [])
    ):
        raise ValueError("one exact native Pod owner required")
    pod = pods[0]
    if pod["spec"]["nodeName"] != candidate["node"]:
        raise ValueError("native route differs")
    containers = pod["spec"]["containers"]
    if (
        len(containers) != 1
        or containers[0]["image"] != candidate["image"]
        or containers[0]["resources"]["requests"].get(candidate["resource_key"]) != "1"
    ):
        raise ValueError("native image or accelerator reservation differs")
    status = pod["status"]["containerStatuses"]
    if len(status) != 1 or status[0].get("restartCount", 0):
        raise ValueError("native container restart or sidecar")
    term = status[0]["state"]["terminated"]
    if term["exitCode"] != 0 or not job.get("status", {}).get("succeeded"):
        raise ValueError("native execution failed")
    scheduled = [
        condition
        for condition in pod["status"].get("conditions", [])
        if condition["type"] == "PodScheduled" and condition["status"] == "True"
    ]
    if len(scheduled) != 1:
        raise ValueError("one observed PodScheduled allocation boundary required")
    allocated = epoch(scheduled[0]["lastTransitionTime"])
    created = epoch(job["metadata"]["creationTimestamp"])
    start, end = epoch(term["startedAt"]), epoch(term["finishedAt"])
    if not created <= allocated <= start <= end:
        raise ValueError("native timestamp order differs")
    reservation = end - allocated
    running = end - start
    return {
        "accepted_at": created,
        "allocated_at": allocated,
        "container_started_at": start,
        "container_finished_at": end,
        "native_jct_seconds": end - created,
        "accelerator_reservation_seconds": reservation,
        "reservation_interval_bounds_seconds": [max(0, reservation - 1), reservation + 1],
        "container_running_seconds": running,
        "container_running_interval_bounds_seconds": [max(0, running - 1), running + 1],
        "pre_container_preparation_seconds": start - allocated,
        "native_jct_interval_bounds_seconds": [max(0, end - created - 1), end - created + 1],
        "timestamp_uncertainty_seconds": 1,
        "image_id": status[0]["imageID"],
        "pod_uid": pod["metadata"]["uid"],
        "job_uid": job["metadata"]["uid"],
        "reservation_unit": "native_extended_resource_device_second",
        "reservation_boundary": "PodScheduled=True.lastTransitionTime-to-container-terminated.finishedAt",
    }


def report_line(log, prefix):
    rows = [json.loads(line.split(prefix, 1)[1]) for line in log.splitlines() if prefix in line]
    if len(rows) != 1:
        raise ValueError("exactly one result envelope/report required")
    return rows[0]


def qualify_result(log, candidate):
    envelope = report_line(log, "RESOURCE_ADVISOR_RESULT ")
    result = envelope["result"]
    if result.get("outcome") != "COMPLETED":
        raise ValueError("completed hardware result required")
    if envelope["digest"] != signature(result) or result.get("evidence_kind") != "hardware":
        raise ValueError("hardware result digest differs")
    if any(result.get(k) != candidate[k] for k in ("workload_signature", "context_signature")):
        raise ValueError("result identity differs")
    measurements = result["measurements"]
    if measurements["work_units"] != candidate["work_units"]:
        raise ValueError("fixed work differs")
    if candidate["group"] == "hailo":
        report = report_line(log, "RESOURCE_ADVISOR_HAILO_REPORT ")
        for key, expected in (
            ("original_model_sha256", candidate["model_digest"][7:]),
            ("hef_sha256", candidate["compiled_digest"][7:]),
            ("manifest_sha256", candidate["input_digest"][7:]),
        ):
            if report[key] != expected:
                raise ValueError("Hailo model/input/artifact differs")
        if report["measured_images"] != 100 or report["device_count"] != 1:
            raise ValueError("Hailo work or device count differs")
        rows = report["predictions"]
        quality = quality_gates(
            [r["prediction"] for r in rows],
            [r["label"] for r in rows],
            [r["reference_top1"] for r in rows],
        )
        if len(rows) != 100 or quality != report["quality"] or not quality["qualified"]:
            raise ValueError("Hailo quality failed")
        if any(report.get(k) != v for k, v in candidate["runtime_versions"].items()):
            raise ValueError("Hailo runtime differs")
    else:
        report = report_line(log, "RESOURCE_ADVISOR_NPU_REPORT ")
        for key in (
            "model_digest",
            "input_digest",
            "work_units",
            "runtime_versions",
            "runner_digest",
        ):
            if report[key] != candidate[key]:
                raise ValueError("NPU model/input/runtime/fixed work differs")
        samples = report["samples_seconds"]
        if (
            len(samples) != candidate["work_units"]
            or any(not math.isfinite(t) or t <= 0 for t in samples)
            or not math.isclose(sum(samples), report["elapsed_seconds"], abs_tol=1e-8)
            or report["quality_value"] != 1
        ):
            raise ValueError("NPU positive measurements or quality failed")
        if candidate["group"] == "mobilint" and (
            len(report["output_digests"]) != 10
            or any(
                "sha256:" + d != report["reference_output_digest"] for d in report["output_digests"]
            )
        ):
            raise ValueError("Mobilint repeatability failed")
        if candidate["group"] == "rockchip" and report["predictions"] != [812] * 10:
            raise ValueError("Rockchip official class failed")
    if (
        not math.isfinite(report["elapsed_seconds"])
        or report["elapsed_seconds"] <= 0
        or not math.isclose(
            report["elapsed_seconds"], measurements["elapsed_seconds"], abs_tol=1e-8
        )
    ):
        raise ValueError("report and result timing differ")
    return report


def hailo_routes(contract, templates):
    """Derive physical routes exclusively from supplied private configuration."""
    capabilities = {cap["ref"]: cap for cap in contract["capabilities"]}
    by_node = {
        template["spec"]["template"]["spec"]["nodeSelector"]["kubernetes.io/hostname"]: template
        for template in templates
    }
    if len(by_node) != len(templates):
        raise ValueError("distinct bound Hailo templates required")
    routes = []
    for candidate in contract["workload"]["candidates"]:
        node = capabilities[candidate["capability_ref"]]["node_ref"]
        template = by_node[node]
        routes.append(
            {
                "node": node,
                "queue": template["metadata"]["labels"]["kueue.x-k8s.io/queue-name"],
                "pod_spec": template["spec"]["template"]["spec"],
                "context": candidate.get("context"),
            }
        )
    return routes


def build_plan(artifacts):
    """Reuse current immutable contracts; preserve fingerprint restrictions."""
    hailo = json.loads((artifacts / "hailo-combined-contracts.json").read_text())
    templates = [
        json.loads((artifacts / source).read_text())
        for source in (
            "ra-hailo-first-qual-20261008-job.json",
            "hailo-second-job.json",
        )
    ]
    candidates = []
    for index, route in enumerate(hailo_routes(hailo, templates)):
        node, queue, context, spec = (route[k] for k in ("node", "queue", "context", "pod_spec"))
        env = {v["name"]: v["value"] for v in spec["containers"][0]["env"]}
        candidates.append(
            {
                "ref": f"hailo-{index + 1}",
                "group": "hailo",
                "node": node,
                "queue": queue,
                "resource_key": "hailo.ai/h8",
                "image": spec["containers"][0]["image"],
                "command": spec["containers"][0]["command"],
                "env": env,
                "pod_spec": copy.deepcopy(spec),
                "work_units": 100,
                "model_digest": hailo["workload"]["identity"]["model_digest"],
                "compiled_digest": "sha256:a1d82e9121c66e772257490cb3af904d1e90fb4387ad5f683fbc5efe1a05f9f7",
                "input_digest": "sha256:7740c3420918e1264024dd41dbe6c0db18a2f1d283789afc89ef3d0db522d30f",
                "runtime_versions": context["runtime_versions"],
                "runner_digest": hailo["workload"]["identity"]["code_digest"],
                "workload_signature": env["RA_WORKLOAD_SIGNATURE"],
                "context_signature": env["RA_CONTEXT_SIGNATURE"],
                "production_profile_reuse": "qualified-context",
            }
        )
    for group, contract_name, template_name in [
        ("mobilint", "mobilint-contracts.json", "mobilint-stack-qual-job.json"),
        ("rockchip", "rockchip-host-contracts-bound.json", "rockchip-host21-qual3-job.json"),
    ]:
        contract = json.loads((artifacts / contract_name).read_text())
        binding = contract["binding"]
        identity, context = binding["identity"], binding["context"]
        variant = RuntimeVariant.model_validate(contract["variant"])
        candidate = Candidate.model_validate(contract["workload"]["candidates"][0])
        ctx_signature = context_signature(candidate, variant)
        pod = json.loads((artifacts / template_name).read_text())["spec"]["template"]["spec"]
        env = {
            "RA_WORKLOAD_SIGNATURE": signature(identity),
            "RA_CONTEXT_SIGNATURE": ctx_signature,
            "RA_CONTEXT_JSON": json.dumps(context),
            "RA_WORK_UNITS": str(identity["work_units"]),
            "RA_INPUT_SHAPE": json.dumps(identity["input_shape"]),
            "RA_PRECISION": identity["precision"],
            "RA_SEED": str(identity["seed"]),
            "RA_EXECUTION_MODE": "observe",
        }
        candidates.append(
            {
                "ref": group,
                "group": group,
                "node": contract["capability"]["node_ref"],
                "queue": "ra-mobilint" if group == "mobilint" else "ra-rockchip-npu",
                "resource_key": contract["capability"]["resource_key"],
                "image": variant.image,
                "command": list(variant.command),
                "env": env,
                "pod_spec": pod,
                "work_units": identity["work_units"],
                "model_digest": identity["model_digest"],
                "compiled_digest": variant.compiled_artifact_digest,
                "input_digest": identity["dataset_version"].split("generated-fixed-input:", 1)[1],
                "runtime_versions": context["runtime_versions"],
                "runner_digest": identity["code_digest"],
                "workload_signature": signature(identity),
                "context_signature": ctx_signature,
                "production_profile_reuse": "blocked-firmware-identity; manual-singleton-observe-only",
            }
        )
    cohorts = []
    generator = random.Random(SEED)
    for group in ("hailo", "mobilint", "rockchip"):
        arms = ["round_robin", "profile_reuse"]
        generator.shuffle(arms)
        for arm in arms:
            cohorts.append({"group": group, "arm": arm, "jobs": 2 if group == "hailo" else 1})
    return {
        "experiment_id": "profile-all-npu-20261008-v3",
        "seed": SEED,
        "candidates": candidates,
        "cohorts": cohorts,
        "scope": "distinct-existing-model-groups; no GPU/NPU same-model ranking",
        "singleton_scope": "same-route manual observe; no placement optimization or production profile promotion",
        "profile_selection_rule": "v2-style frozen native service + synthetic cohort backlog; no live queue",
        "global_deadline_seconds": 600,
    }


def manifest(candidate, name):
    pod = copy.deepcopy(candidate["pod_spec"])
    pod["nodeSelector"]["kubernetes.io/hostname"] = candidate["node"]
    container = pod["containers"][0]
    container["image"], container["command"] = candidate["image"], candidate["command"]
    env = dict(candidate["env"])
    env.update(
        RA_JOB_ID=name,
        RA_ATTEMPT_ID=name,
        RA_EPOCH="1",
        RA_ARTIFACT_PREFIX=f"profile-all-20261008-v3/npu/{name}",
    )
    container["env"] = [{"name": k, "value": v} for k, v in env.items()]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "labels": {
                "kueue.x-k8s.io/queue-name": candidate["queue"],
                "resource-advisor/experiment": "profile-all-npu-20261008-v3",
            },
        },
        "spec": {
            "suspend": True,
            "backoffLimit": 0,
            "activeDeadlineSeconds": 180 if candidate["group"] == "hailo" else 120,
            "template": {
                "metadata": {"annotations": {"sidecar.istio.io/inject": "false"}},
                "spec": pod,
            },
        },
    }


class NativeExecutor:
    def __init__(self, directory, deadline):
        self.directory = directory
        self.deadline = deadline
        self.owned = set()

    def kube(self, args, *, data=None):
        timeout = max(1, min(30, self.deadline - time.monotonic()))
        run = subprocess.run(
            ["rtk", "proxy", "kubectl", *args],
            input=data,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if run.returncode:
            raise RuntimeError(run.stderr.strip() or "kubectl failed")
        return run.stdout

    def receipt(self, name):
        job = json.loads(self.kube(["get", "job", name, "-n", NAMESPACE, "-o", "json"]))
        pods = json.loads(
            self.kube(["get", "pods", "-n", NAMESPACE, "-l", "job-name=" + name, "-o", "json"])
        )
        root = self.directory / "jobs" / name
        write_json(root / "job.json", job)
        write_json(root / "pods.json", pods)
        return job, pods["items"]

    def workloads(self, name, uid):
        raw = json.loads(
            self.kube(["get", "workloads.kueue.x-k8s.io", "-n", NAMESPACE, "-o", "json"])
        )
        owned = [
            w
            for w in raw["items"]
            if any(
                o.get("kind") == "Job" and o.get("uid") == uid
                for o in w["metadata"].get("ownerReferences", [])
            )
        ]
        write_json(self.directory / "jobs" / name / "workloads.json", {"items": owned})
        return owned

    def run_job(self, candidate, name):
        root = self.directory / "jobs" / name
        doc = manifest(candidate, name)
        write_json(root / "manifest.json", doc)
        intent = {
            "candidate_ref": candidate["ref"],
            "name": name,
            "request_started_at": time.time(),
            "manifest_sha256": signature(doc),
        }
        write_json(root / "submit-intent.json", intent)
        self.owned.add(name)
        row = {
            "candidate_ref": candidate["ref"],
            "name": name,
            "qualified": False,
            "outcome": "UNCONFIRMED",
            "group": candidate["group"],
        }
        try:
            created = json.loads(
                self.kube(["create", "-f", "-", "-o", "json"], data=json.dumps(doc))
            )
            intent["request_finished_at"] = time.time()
            write_json(root / "created.json", created)
            write_json(root / "submit-intent.json", intent)
            job_deadline = min(
                self.deadline, time.monotonic() + doc["spec"]["activeDeadlineSeconds"] + 30
            )
            while time.monotonic() < job_deadline:
                job, pods = self.receipt(name)
                complete = any(
                    c["status"] == "True" and c["type"] in {"Complete", "Failed"}
                    for c in job.get("status", {}).get("conditions", [])
                )
                if (
                    complete
                    and pods
                    and all(p["status"]["phase"] in {"Succeeded", "Failed"} for p in pods)
                ):
                    break
                time.sleep(1)
            else:
                raise TimeoutError("native Job deadline exceeded")
            logs = []
            for pod in pods:
                log = self.kube(["logs", pod["metadata"]["name"], "-n", NAMESPACE, "--timestamps"])
                (root / (pod["metadata"]["name"] + ".log")).write_text(log)
                logs.append(log)
            log = "\n".join(logs)
            row["native_status"] = job.get("status", {})
            row["timing"] = native_timing(job, pods, candidate)
            row["report"] = qualify_result(log, candidate)
            row["qualified"], row["outcome"] = True, "COMPLETED"
            row["log_sha256"] = hashlib.sha256(log.encode()).hexdigest()
            row["service_seconds"] = max(
                row["timing"]["native_jct_seconds"], row["report"]["elapsed_seconds"]
            )
            while time.monotonic() < job_deadline:
                workloads = self.workloads(name, job["metadata"]["uid"])
                if workloads and all(
                    any(
                        c["type"] == "Finished" and c["status"] == "True"
                        for c in w.get("status", {}).get("conditions", [])
                    )
                    for w in workloads
                ):
                    row["native_queue_drained"] = True
                    break
                time.sleep(1)
            else:
                raise TimeoutError("terminal workload release not observed")
        except (
            KeyError,
            TypeError,
            ValueError,
            OSError,
            RuntimeError,
            subprocess.SubprocessError,
        ) as error:
            row["qualified"] = False
            row["error"] = type(error).__name__ + ": " + str(error)
            try:
                job, pods = self.receipt(name)
                row["native_status"] = job.get("status", {})
                for pod in pods:
                    try:
                        log = self.kube(
                            ["logs", pod["metadata"]["name"], "-n", NAMESPACE, "--timestamps"]
                        )
                        (root / (pod["metadata"]["name"] + ".log")).write_text(log)
                    except (
                        KeyError,
                        TypeError,
                        ValueError,
                        OSError,
                        RuntimeError,
                        subprocess.SubprocessError,
                    ):
                        pass
            except (
                KeyError,
                TypeError,
                ValueError,
                OSError,
                RuntimeError,
                subprocess.SubprocessError,
            ):
                pass
        write_json(root / "result.json", row)
        print(
            json.dumps({k: row[k] for k in ("name", "candidate_ref", "qualified", "outcome")}),
            flush=True,
        )
        return row

    def stop_owned(self):
        for name in sorted(self.owned):
            try:
                job, pods = self.receipt(name)
                if not any(
                    c["status"] == "True" and c["type"] in {"Complete", "Failed"}
                    for c in job.get("status", {}).get("conditions", [])
                ):
                    raw = self.kube(
                        ["delete", "job", name, "-n", NAMESPACE, "--wait=true", "--timeout=20s"]
                    )
                    (self.directory / "jobs" / name / "cancellation.log").write_text(raw)
            except (
                KeyError,
                TypeError,
                ValueError,
                OSError,
                RuntimeError,
                subprocess.SubprocessError,
            ) as error:
                write_json(
                    self.directory / "jobs" / name / "cleanup-error.json", {"error": str(error)}
                )


def run(directory, artifacts):
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "capture.json").exists():
        raise ValueError("capture already exists; never duplicate native submissions")
    plan = build_plan(artifacts)
    write_json(directory / "plan.json", plan)
    capture = {
        "plan_sha256": signature(plan),
        "started_at": time.time(),
        "profiles": [],
        "cohorts": [],
    }
    write_json(directory / "capture.json", capture)
    executor = NativeExecutor(directory, time.monotonic() + plan["global_deadline_seconds"])
    try:
        profiles = {}
        for candidate in plan["candidates"]:
            row = executor.run_job(candidate, "profile-all-npu-v3-profile-" + candidate["ref"])
            capture["profiles"].append(row)
            write_json(directory / "capture.json", capture)
            if not row["qualified"]:
                raise RuntimeError("fresh profile failed; no repair/retry or main submissions")
            profiles[candidate["ref"]] = {
                "service_seconds": row["service_seconds"],
                "source": row["name"],
                "elapsed_seconds": row["report"]["elapsed_seconds"],
            }
        capture["profiles_frozen_at"] = time.time()
        capture["frozen_profiles"] = profiles
        write_json(
            directory / "frozen-profiles.json",
            {"frozen_at": capture["profiles_frozen_at"], "profiles": profiles},
        )
        write_json(directory / "capture.json", capture)
        for cohort in plan["cohorts"]:
            candidates = {c["ref"]: c for c in plan["candidates"] if c["group"] == cohort["group"]}
            refs = list(candidates)
            costs = {ref: profiles[ref] for ref in refs}
            backlog, selections = {}, []
            for index in range(cohort["jobs"]):
                selected = choose_candidate(cohort["arm"], refs, costs, backlog, index)
                selections.append(
                    {"candidate_ref": selected, "backlog_before": dict(backlog), "index": index}
                )
                backlog[selected] = backlog.get(selected, 0) + costs[selected]["service_seconds"]
            record = {
                **cohort,
                "choice_started_at": time.time(),
                "choices": selections,
                "frozen_profiles_sha256": signature(profiles),
            }
            key = cohort["group"] + "-" + cohort["arm"].replace("_", "-")
            write_json(directory / "cohorts" / (key + "-intent.json"), record)
            start = time.monotonic()
            with ThreadPoolExecutor(max_workers=cohort["jobs"]) as pool:
                futures = [
                    pool.submit(
                        executor.run_job,
                        candidates[s["candidate_ref"]],
                        f"profile-all-npu-v3-{key}-{s['index']}",
                    )
                    for s in selections
                ]
                rows = [future.result() for future in futures]
            record["jobs"], record["client_wall_seconds"] = rows, time.monotonic() - start
            capture["cohorts"].append(record)
            write_json(directory / "capture.json", capture)
            if not all(row["qualified"] for row in rows):
                raise RuntimeError("main native/quality failure; no repair or retry")
        capture["completed_at"], capture["status"] = time.time(), "COMPLETED"
    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        capture["status"], capture["error"], capture["stopped_at"] = (
            "STOPPED",
            str(error),
            time.time(),
        )
    finally:
        executor.stop_owned()
        write_json(directory / "capture.json", capture)
    summary = []
    for cohort in capture["cohorts"]:
        rows = cohort["jobs"]
        if all(r["qualified"] for r in rows):
            jct = [r["timing"]["native_jct_seconds"] for r in rows]
            native_wall = max(r["timing"]["container_finished_at"] for r in rows) - min(
                r["timing"]["accepted_at"] for r in rows
            )
            summary.append(
                {
                    "group": cohort["group"],
                    "arm": cohort["arm"],
                    "jobs": len(rows),
                    "mean_jct_seconds": statistics.mean(jct),
                    "p95_jct_seconds": sorted(jct)[math.ceil(len(jct) * 0.95) - 1],
                    "compute_seconds_sum": sum(r["report"]["elapsed_seconds"] for r in rows),
                    "reservation_seconds_sum": sum(
                        r["timing"]["accelerator_reservation_seconds"] for r in rows
                    ),
                    "container_running_seconds_sum": sum(
                        r["timing"]["container_running_seconds"] for r in rows
                    ),
                    "native_wall_seconds": native_wall,
                    "native_throughput_jobs_per_second": len(rows) / native_wall
                    if native_wall > 0
                    else None,
                    "placement_scope": "two-device descriptive"
                    if cohort["group"] == "hailo"
                    else "manual-singleton-no-placement-gain",
                }
            )
    write_json(
        directory / "summary.json",
        {
            "status": capture["status"],
            "cohorts": summary,
            "profiling_jobs": len(capture["profiles"]),
            "limitations": plan["scope"]
            + "; n=one cohort/arm; native timestamps whole seconds; firmware gates unchanged",
        },
    )
    hashes = {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in directory.rglob("*")
        if p.is_file() and p.name != "checksums.json"
    }
    write_json(directory / "checksums.json", hashes)
    return capture


def correct_metrics(directory):
    """Recalculate allocation costs from saved receipts without native actions.

    Preserve execution-time profiles, choices and raw files. Their selection
    costs were native-JCT proxies, whereas the old usage metric counted only
    the container-running interval. This correction changes usage accounting.
    """
    capture_path = directory / "capture.json"
    plan_path = directory / "plan.json"
    capture = json.loads(capture_path.read_text())
    plan = json.loads(plan_path.read_text())
    backup = directory / "pre-review-metric-output"
    backup.mkdir(exist_ok=True)
    for name in ("summary.json", "rows.csv", "checksums.json", "recording-scope.json"):
        source, target = directory / name, backup / name
        if source.exists() and not target.exists():
            shutil.copyfile(source, target)
    candidates = {candidate["ref"]: candidate for candidate in plan["candidates"]}
    sources = [capture_path, plan_path, directory / "frozen-profiles.json"]
    timings, corrected_rows, changes = {}, [], []
    cohorts = [("profiling", "profiling", capture["profiles"])] + [
        ("main", cohort["arm"], cohort["jobs"]) for cohort in capture["cohorts"]
    ]
    for phase, arm, rows in cohorts:
        for row in rows:
            root = directory / "jobs" / row["name"]
            job_path, pod_path = root / "job.json", root / "pods.json"
            job, pods = json.loads(job_path.read_text()), json.loads(pod_path.read_text())["items"]
            timing = native_timing(job, pods, candidates[row["candidate_ref"]])
            if timing["native_jct_seconds"] != row["timing"]["native_jct_seconds"]:
                raise ValueError("correction unexpectedly changed the native JCT endpoint")
            timings[row["name"]] = timing
            sources.extend(
                [job_path, pod_path, root / "submit-intent.json", root / "manifest.json"]
            )
            prior = row["timing"]["accelerator_reservation_seconds"]
            changes.append(
                {
                    "attempt": row["name"],
                    "prior_container_running_seconds": prior,
                    "corrected_pod_scheduled_to_end_seconds": timing[
                        "accelerator_reservation_seconds"
                    ],
                    "missing_pre_container_seconds": timing["pre_container_preparation_seconds"],
                }
            )
            report = row["report"]
            corrected_rows.append(
                {
                    "phase": phase,
                    "arm": arm,
                    "group": row["group"],
                    "candidate": row["candidate_ref"],
                    "attempt": row["name"],
                    "qualified": row["qualified"],
                    "native_jct_seconds_point": timing["native_jct_seconds"],
                    "measured_inference_seconds": report["elapsed_seconds"],
                    "pod_scheduled_at": timing["allocated_at"],
                    "container_started_at": timing["container_started_at"],
                    "container_finished_at": timing["container_finished_at"],
                    "allocated_device_seconds_point": timing["accelerator_reservation_seconds"],
                    "allocated_seconds_lower": timing["reservation_interval_bounds_seconds"][0],
                    "allocated_seconds_upper": timing["reservation_interval_bounds_seconds"][1],
                    "container_running_seconds_point": timing["container_running_seconds"],
                    "pre_container_preparation_seconds_point": timing[
                        "pre_container_preparation_seconds"
                    ],
                    "native_clock_offset_qualified": False,
                    "quality_value": report.get(
                        "quality_value", report.get("quality", {}).get("accuracy")
                    ),
                }
            )
    summary = json.loads((backup / "summary.json").read_text())
    for output in summary["cohorts"]:
        cohort = next(
            c
            for c in capture["cohorts"]
            if (c["group"], c["arm"]) == (output["group"], output["arm"])
        )
        current = [timings[row["name"]] for row in cohort["jobs"]]
        output["prior_container_running_seconds_sum"] = output["reservation_seconds_sum"]
        output["reservation_seconds_sum"] = sum(
            t["accelerator_reservation_seconds"] for t in current
        )
        output["container_running_seconds_sum"] = sum(
            t["container_running_seconds"] for t in current
        )
        output["pre_container_preparation_seconds_sum"] = sum(
            t["pre_container_preparation_seconds"] for t in current
        )
    summary["allocation_boundary"] = (
        "PodScheduled=True.lastTransitionTime-to-container-terminated.finishedAt"
    )
    summary["profiling_allocated_device_seconds_point"] = sum(
        timings[row["name"]]["accelerator_reservation_seconds"] for row in capture["profiles"]
    )
    summary["main_allocated_device_seconds_point"] = sum(
        timings[row["name"]]["accelerator_reservation_seconds"]
        for c in capture["cohorts"]
        for row in c["jobs"]
    )
    summary["limitations"] += (
        "; allocation bounds cover whole-second quantization only, not unqualified cross-host clock offsets; pre-PodScheduled Kueue quota hold excluded"
    )
    write_json(directory / "corrected-summary.json", summary)
    import csv

    with (directory / "corrected-rows.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=corrected_rows[0].keys())
        writer.writeheader()
        writer.writerows(corrected_rows)
    source = directory / "metric-recalculation.py"
    shutil.copyfile(Path(__file__), source)
    correction = {
        "native_actions_performed": False,
        "old_boundary": "container.startedAt-to-container.finishedAt",
        "new_boundary": summary["allocation_boundary"],
        "jct_compute_and_quality_unchanged": True,
        "execution_profiles_and_choices_unchanged": True,
        "selection_cost_at_execution": "max(native Job-creation-to-container-end JCT, measured inference); unchanged frozen-profiles.json",
        "cost_correction_scope": "usage accounting; old container-running cost omitted PodScheduled-to-container-start allocation",
        "original_metric_outputs": "pre-review-metric-output/",
        "job_changes": changes,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "unchanged_input_sha256": {
            str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sources
        },
    }
    write_json(directory / "metric-correction.json", correction)
    scope_path = directory / "recording-scope.json"
    scope = (
        json.loads((backup / "recording-scope.json").read_text())
        if (backup / "recording-scope.json").exists()
        else {}
    )
    scope["previous_reservation_scope"] = scope.get("reservation_scope")
    scope["reservation_scope"] = (
        "PodScheduled-to-container-end native extended-resource request1 duration; excludes earlier Kueue quota hold and physical activity"
    )
    scope["metric_correction"] = {
        key: correction[key]
        for key in (
            "old_boundary",
            "new_boundary",
            "execution_profiles_and_choices_unchanged",
            "selection_cost_at_execution",
        )
    }
    scope["current_metrics"] = {"summary": "corrected-summary.json", "rows": "corrected-rows.csv"}
    write_json(scope_path, scope)
    write_json(
        directory / "checksums.json",
        {
            str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.rglob("*"))
            if path.is_file() and path != directory / "checksums.json"
        },
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument(
        "--correct-metrics-only",
        action="store_true",
        help="Saved-receipt recalculation, no native submissions",
    )
    args = parser.parse_args()
    if args.correct_metrics_only:
        summary = correct_metrics(args.directory)
        print(
            json.dumps({"status": summary["status"], "corrected_cohorts": len(summary["cohorts"])})
        )
        return 0
    if args.artifacts is None:
        parser.error("--artifacts is required for native execution")
    result = run(args.directory, args.artifacts)
    print(
        json.dumps(
            {
                "status": result["status"],
                "profiles": len(result["profiles"]),
                "cohorts": len(result["cohorts"]),
            }
        )
    )
    return 0 if result["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
