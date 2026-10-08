"""Independent saved-evidence audit for the preregistered JCT comparison.

Native completion endpoints differ between Kubernetes container exit and Slurm
allocation EndTime. Shared accelerator slot seconds are never physical GPU time.
"""

import ast
import hashlib
import json
import math
import statistics
from datetime import UTC, datetime
from pathlib import Path

from resource_advisor import jct_selection

ARMS = ("round_robin", "profile_only", "profile_queue")
LOADS = {"sparse": 3.0, "moderate": 0.5, "burst": 0.0}
FROZEN_SCHEDULES = {
    12: "8b393225e42b905d7302d8c11029db401b2c8ca7f43561b747a0622c0e804355",
    6: "bb992b5f69dc7fe7a96fa085785e9cff604ed50df820b61081fb30fa0d9e88f8",
}
FROZEN_ROUTES = {
    "rtx5060": ("kubernetes", "etri-ser0001-cg0msb", "nvidia.com/gpu", 1),
    "spark": ("kubernetes", "etri-ser0003-cg0ms0", "nvidia.com/gpu", 1),
    "jetson-nano": ("kubernetes", "etri-dev0001-jetorn", "nvidia.com/gpu.shared", 2),
    "jetson-agx": ("kubernetes", "etri-dev0005-jetagx", "nvidia.com/gpu.shared", 2),
    "rtx5080": ("kubernetes", "etri-ser0002-cgnmsb", "nvidia.com/gpu", 1),
    "slurm-orin": ("slurm", "slurm-w2", "gres/gpu:orin_nano", 1),
}
FROZEN_WORKLOAD = {
    "fixture_sha256": "644a0c537bd997d18ce29e22ca5074ba5d8dccf340b8b39a2fb435de2113ba25",
    "source_sha256": "d1f5a96f0a8896d4710c77c698e883df3cd0c09fb1cd4ad2037f6e118093bd6d",
    "kernel_sha256": "241965dbd209e40184a62db4d13e54e7b3f1ec2cda43dda30fe31da17baef700",
}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _epoch(value, timezone=None):
    if _finite(value):
        return float(value)
    _require(isinstance(value, str), "missing native timestamp")
    if value.isdigit():
        return float(value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("malformed native timestamp") from exc
    if parsed.tzinfo is None:
        _require(timezone == "UTC", "naive Slurm timestamp without UTC provenance")
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _validate_plan(plan):
    _require(
        plan["paired_blocks"] == 3 and plan["jobs_per_cohort"] in (6, 12),
        "changed preregistered replication",
    )
    _require(
        plan["main_rounds"] == 16384 and plan["loads"] == LOADS,
        "changed preregistered work or loads",
    )
    _require(plan["seed"] == 20261008, "changed preregistered seed")
    _require(
        all(plan.get(k) == v for k, v in FROZEN_WORKLOAD.items()),
        "changed frozen workload identity",
    )
    _require(
        plan["resources"] == {"cpu": 1, "memory_mib": 512, "accelerators": 1},
        "changed preregistered resources",
    )
    pool = plan["pool"]
    _require(
        len(pool) == 6 and len({n["ref"] for n in pool}) == 6, "changed or duplicate candidate pool"
    )
    _require(
        sum(n["backend"] == "kubernetes" for n in pool) == 5
        and sum(n["backend"] == "slurm" for n in pool) == 1,
        "complete Kubernetes and Slurm pool required",
    )
    _require(len({(n["backend"], n["node"]) for n in pool}) == 6, "duplicate physical candidate")
    _require(
        {n["ref"]: (n["backend"], n["node"], n["resource_key"], n["nominal_slots"]) for n in pool}
        == FROZEN_ROUTES,
        "changed preregistered resource route/capacity",
    )


def _native_identity(job, node, plan):
    receipt = job["native_receipt"]
    _require(receipt["attempt_id"] == job["attempt_id"], "native attempt identity mismatch")
    _require(receipt["termination"]["exitCode"] == 0, "native execution failed")
    if node["backend"] == "kubernetes":
        native_job, pod = receipt["job"], receipt["pod"]
        metadata = native_job["metadata"]
        _require(metadata["name"] == job["attempt_id"] == job["native_id"], "foreign native Job")
        owners = [o for o in pod["metadata"]["ownerReferences"] if o["kind"] == "Job"]
        _require(
            len(owners) == 1
            and owners[0]["uid"] == metadata["uid"]
            and owners[0]["name"] == metadata["name"],
            "foreign native Pod owner",
        )
        _require(pod["spec"]["nodeName"] == node["node"], "wrong execution node")
        # The declared K8s endpoint is successful container exit. Pod/Job phase
        # reconciliation can lag that endpoint in the same native snapshot.
        _require(
            pod["status"]["phase"] in {"Running", "Succeeded"},
            "native Pod failed or lacks terminal container evidence",
        )
        _require(not native_job.get("status", {}).get("failed"), "native Job failed")
        for spec in (pod["spec"], native_job["spec"]["template"]["spec"]):
            containers = spec["containers"]
            _require(len(containers) == 1, "ambiguous native containers")
            container = containers[0]
            _require(container["image"] == node["image"], "wrong execution image")
            for field in ("requests", "limits"):
                resources = container["resources"][field]
                _require(
                    resources == {"cpu": "1", "memory": "512Mi", node["resource_key"]: "1"},
                    "wrong native resources",
                )
            _require(
                spec.get("runtimeClassName") == node.get("runtime_class"),
                "wrong native RuntimeClass",
            )
        statuses = pod["status"]["containerStatuses"]
        _require(
            len(statuses) == 1 and statuses[0].get("restartCount", 0) == 0,
            "retry or ambiguous native container",
        )
        terminated = statuses[0]["state"]["terminated"]
        _require(terminated == receipt["termination"], "changed container termination")
        scheduled = [
            c
            for c in pod["status"]["conditions"]
            if c["type"] == "PodScheduled" and c["status"] == "True"
        ]
        _require(len(scheduled) == 1, "unknown native schedule boundary")
        accepted = _epoch(metadata["creationTimestamp"])
        started = _epoch(scheduled[0]["lastTransitionTime"])
        finished = _epoch(terminated["finishedAt"])
        identity = ("kubernetes", metadata["uid"])
    else:
        accounting = receipt.get("native_accounting")
        if accounting is not None:
            _require(
                accounting["account"] == "ra-lab" and accounting["qos"] == "ra-normal",
                "foreign Slurm account or QOS",
            )
            _require(str(job["native_id"]).isdigit(), "Slurm step is not a primary allocation")
            fields = {
                "JobId": accounting["native_id"],
                "JobName": accounting["name"],
                "JobState": accounting["state"],
                "ExitCode": accounting["exit_code"],
                "NodeList": accounting["nodes"],
                "ReqTRES": accounting["req_tres"],
                "AllocTRES": accounting["alloc_tres"],
                "SubmitTime": accounting["submit"],
                "StartTime": accounting["start"],
                "EndTime": accounting["end"],
            }
        else:
            fields = receipt["native_fields"]
        _require(
            receipt["native_state"] == fields["JobState"] == "COMPLETED"
            and fields["ExitCode"] == "0:0",
            "native Slurm job did not succeed",
        )
        _require(
            str(job["native_id"]) == str(receipt["native_id"]) == fields["JobId"]
            and fields["JobName"] == job["attempt_id"],
            "foreign native Slurm job",
        )
        _require(fields["NodeList"] == node["node"], "wrong Slurm execution node")
        if accounting is None:
            _require(
                fields["NumCPUs"] == "1" and fields["MinMemoryNode"] == "512M",
                "wrong Slurm CPU or memory",
            )
        for field in ("ReqTRES", "AllocTRES"):
            tres = dict(term.split("=", 1) for term in fields[field].split(","))
            _require(
                tres.get("cpu") == "1"
                and tres.get("mem") == "512M"
                and tres.get("node") == "1"
                and tres.get("gres/gpu") == "1",
                "wrong native Slurm allocation",
            )
        if accounting is None:
            _require(fields["TresPerNode"] == node["resource_key"] + ":1", "wrong Slurm GRES")
        timezone = receipt.get("native_timezone", plan.get("slurm_timezone"))
        accepted, started, finished = (
            _epoch(fields[k], timezone) for k in ("SubmitTime", "StartTime", "EndTime")
        )
        if accounting is not None:
            _require(
                accounting["accepted_at"] == accepted
                and accounting["started_at"] == started
                and accounting["finished_at"] == finished
                and int(accounting["elapsed_raw"]) == finished - started,
                "changed primary Slurm accounting clock",
            )
        identity = ("slurm", fields["JobId"], accepted)
    _require(accepted <= started <= finished, "reversed native clock boundaries")
    _require(
        job["accepted_at"] == accepted
        and job["started_at"] == started
        and job["finished_at"] == finished,
        "changed native timestamp boundaries",
    )
    _require(
        receipt["scheduled_at"] == started and receipt["finished_at"] == finished,
        "changed native receipt boundary",
    )
    _require(
        job["gpu_seconds"] == finished - started and receipt["gpu_seconds"] == finished - started,
        "fabricated reservation cost",
    )
    return identity


def _validate_jobs(capture):
    plan = capture["plan"]
    _validate_plan(plan)
    nodes = {n["ref"]: n for n in plan["pool"]}
    sources = {j["attempt_id"]: j for j in capture["qualifications"]}
    _require(
        len(sources) == len(capture["qualifications"]) and sources, "duplicate or missing profiles"
    )
    _require(
        set(capture["profiling"]["source_attempts"]) == set(sources),
        "incomplete profile cost provenance",
    )
    _require({j["candidate_ref"] for j in sources.values()} == set(nodes), "unqualified candidate")
    expected = {(load, arm, block) for load in LOADS for arm in ARMS for block in range(3)}
    blocks, seen, native_seen = set(), set(), set()
    main_start = min(j["request_started_at"] for c in capture["cohorts"] for j in c["jobs"])
    for cohort in [None, *capture["cohorts"]]:
        if cohort is None:
            jobs = list(sources.values())
        else:
            block = (cohort["load"], cohort["arm"], cohort["block"])
            _require(block in expected and block not in blocks, "unknown or duplicate cohort")
            blocks.add(block)
            jobs = cohort["jobs"]
            _require(
                len(jobs) == plan["jobs_per_cohort"]
                and _finite(cohort["wall_seconds"])
                and cohort["wall_seconds"] > 0,
                "incomplete preregistered cohort",
            )
        for job in jobs:
            ref = job["attempt_id"]
            _require(ref not in seen, "duplicate or leaked main attempt")
            seen.add(ref)
            node = nodes.get(job["candidate_ref"])
            _require(
                node is not None and job["backend"] == node["backend"],
                "unknown or mismatched candidate/backend",
            )
            _require(
                all(
                    _finite(job[key])
                    for key in (
                        "accepted_at",
                        "started_at",
                        "finished_at",
                        "gpu_seconds",
                        "request_started_at",
                    )
                ),
                "unknown job clock or cost",
            )
            _require(
                job["accepted_at"] <= job["started_at"] <= job["finished_at"],
                "reversed clock boundaries",
            )
            _require(
                job["outcome"] == "SUCCEEDED" and job["quality"] == 1,
                "failed or unqualified main evidence; retain, do not promote",
            )
            detail = job["details"]
            _require(
                detail["measured"] is True
                and detail["evidence_kind"] == "hardware"
                and detail["outcome"] == "COMPLETED",
                "non-hardware or failed result",
            )
            _require(
                all(
                    detail[k] == plan["fixture_sha256"]
                    for k in ("fixture_sha256", "model_digest", "input_digest")
                )
                and detail["kernel_sha256"] == plan["kernel_sha256"],
                "wrong workload identity",
            )
            rounds = plan["qualification_rounds"] if cohort is None else plan["main_rounds"]
            _require(
                detail["rounds"] == rounds
                and job["requested_work"] == rounds
                and detail["batch_size"] == 256
                and detail["images"] == rounds * 256
                and job["units"] == rounds * 256,
                "wrong fixed work",
            )
            _require(
                detail["quality"] == 1 and detail["accuracy"] == 0.98046875,
                "wrong reference quality",
            )
            _require(
                detail["arch"] == node["arch"] and detail["runtime_versions"],
                "wrong or unknown runtime identity",
            )
            _require(
                _finite(detail["elapsed_seconds"])
                and detail["elapsed_seconds"] > 0
                and detail["compute_started_at"] <= detail["compute_finished_at"],
                "unknown or reversed compute clock",
            )
            _require(
                detail["compute_started_at"] >= job["started_at"] - 1
                and detail["compute_finished_at"] <= job["finished_at"] + 1,
                "compute lies outside native allocation and one-second uncertainty",
            )
            if "round_seconds" in detail:
                seconds = detail["round_seconds"]
                _require(
                    isinstance(seconds, list)
                    and len(seconds) == rounds
                    and all(_finite(value) and value >= 0 for value in seconds)
                    and math.isclose(
                        math.fsum(seconds), detail["elapsed_seconds"], rel_tol=0, abs_tol=1e-9
                    ),
                    "incomplete or fabricated synchronized compute rounds",
                )
            identity = _native_identity(job, node, plan)
            _require(identity not in native_seen, "duplicate native allocation")
            native_seen.add(identity)
            result = job["native_receipt"]["result"]
            _require(
                all(
                    result.get(k) == v
                    for k, v in detail.items()
                    if k not in ("model_digest", "input_digest")
                ),
                "changed raw native result",
            )
            if cohort is None:
                _require(job["finished_at"] < main_start, "profile/main leakage or tuning")
            elif cohort["arm"] == "round_robin":
                _require(not job.get("profile_source"), "Round Robin consumed profile")
            else:
                source = sources.get(job.get("profile_source"))
                _require(
                    source is not None and source["candidate_ref"] == job["candidate_ref"],
                    "unqualified or leaked profile source",
                )
    _require(blocks == expected, "incomplete preregistered trace")
    _require(
        capture["profiling"]["gpu_seconds"] == sum(j["gpu_seconds"] for j in sources.values()),
        "incomplete or fabricated profiling cost",
    )
    _require(
        _finite(capture["profiling"]["wall_seconds"]) and capture["profiling"]["wall_seconds"] > 0,
        "unknown profiling wall cost",
    )


def _reservation_key(job, plan):
    node = next(n for n in plan["pool"] if n["ref"] == job["candidate_ref"])
    if node["backend"] == "slurm":
        return "slurm_gres"
    return (
        "kubernetes_shared_slot"
        if node["resource_key"].endswith(".shared")
        else "kubernetes_exclusive"
    )


def _metrics(cohorts, plan):
    jobs = [j for c in cohorts for j in c["jobs"]]
    jcts = sorted(j["finished_at"] - j["accepted_at"] for j in jobs)
    native_wall = sum(
        max(j["finished_at"] for j in c["jobs"]) - min(j["accepted_at"] for j in c["jobs"])
        for c in cohorts
    )
    cost = dict.fromkeys(("kubernetes_exclusive", "kubernetes_shared_slot", "slurm_gres"), 0)
    for job in jobs:
        cost[_reservation_key(job, plan)] += job["gpu_seconds"]
    return {
        "cohort_repetitions": len(cohorts),
        "jobs": len(jobs),
        "mean_jct_seconds": statistics.mean(jcts),
        "p95_jct_seconds": jcts[math.ceil(len(jcts) * 0.95) - 1],
        "mean_wait_seconds": statistics.mean(j["started_at"] - j["accepted_at"] for j in jobs),
        "native_wall_seconds": native_wall,
        "client_wall_seconds": sum(
            c.get("client_wall_seconds", c["wall_seconds"]) for c in cohorts
        ),
        "native_throughput_jobs_per_second": len(jobs) / native_wall,
        "native_throughput_images_per_second": sum(j["units"] for j in jobs) / native_wall,
        "mixed_accelerator_slot_seconds": sum(cost.values()),
        "reservation_seconds": cost,
        "physical_gpu_hours": None,
        "synchronized_inference_seconds": sum(j["details"]["elapsed_seconds"] for j in jobs),
        "mean_acceptance_to_compute_end_seconds": statistics.mean(
            j["details"]["compute_finished_at"] - j["accepted_at"] for j in jobs
        ),
        "continuous_pool_gpu_utilization_percent": None,
    }


def summarize_capture(capture):
    """Summarize complete native cohorts; replication unit is the cohort."""
    try:
        _validate_jobs(capture)
    except (KeyError, TypeError, StopIteration) as exc:
        raise ValueError("incomplete native evidence") from exc
    plan = capture["plan"]
    cohorts = capture["cohorts"]
    profiles = capture["qualifications"]
    summary = {
        "loads": {
            load: {
                arm: _metrics([c for c in cohorts if c["load"] == load and c["arm"] == arm], plan)
                for arm in ARMS
            }
            for load in LOADS
        },
        "cohorts": [
            {"load": c["load"], "arm": c["arm"], "block": c["block"], **_metrics([c], plan)}
            for c in cohorts
        ],
        "profile_upfront": {
            "successful_native_attempts": len(profiles),
            "mixed_accelerator_slot_seconds": sum(j["gpu_seconds"] for j in profiles),
            "reservation_seconds": _metrics(
                [{"jobs": profiles, "wall_seconds": capture["profiling"]["wall_seconds"]}], plan
            )["reservation_seconds"],
            "wall_seconds": capture["profiling"]["wall_seconds"],
            "cpu_model_training_cost": "shared preparation; unknown, not zero",
            "physical_gpu_hours": None,
        },
        "main_jobs": sum(len(c["jobs"]) for c in cohorts),
        "replication_unit": "policy cohort; native jobs within each cohort are subsamples",
        "jobs_per_cohort": plan["jobs_per_cohort"],
        "expected_main_jobs": 27 * plan["jobs_per_cohort"],
        "statistical_significance_established": False,
        "p95_method": "nearest rank over native jobs; descriptive, cohort repetitions n=3",
        "native_jct_boundary": "acceptance to K8s container exit / Slurm allocation EndTime",
        "profile_only_definition": "v2 frozen service estimate plus synthetic within-cohort backlog",
        "evidence_complete": False,
    }
    summary["sensors_by_native_job"] = [_sensor_windows(j) for c in cohorts for j in c["jobs"]]
    summary["net_cost"] = {}
    queue = sum(
        summary["loads"][load]["profile_queue"]["mixed_accelerator_slot_seconds"] for load in LOADS
    )
    upfront = summary["profile_upfront"]["mixed_accelerator_slot_seconds"]
    for comparator in ARMS[:2]:
        control = sum(
            summary["loads"][load][comparator]["mixed_accelerator_slot_seconds"] for load in LOADS
        )
        control_with_profile = control + (upfront if comparator == "profile_only" else 0)
        queue_with_profile = queue + upfront
        difference = control_with_profile - queue_with_profile
        bound = 18 * plan["jobs_per_cohort"] + (len(profiles) if comparator == "round_robin" else 0)
        summary["net_cost"]["profile_queue_vs_" + comparator] = {
            "control_main_mixed_slot_seconds": control,
            "control_with_profile_mixed_slot_seconds": control_with_profile,
            "queue_main_mixed_slot_seconds": queue,
            "queue_with_profile_mixed_slot_seconds": queue_with_profile,
            "profile_upfront_charged_seconds": upfront,
            "observed_net_mixed_slot_reduction_percent": (
                1 - queue_with_profile / control_with_profile
            )
            * 100,
            "net_difference_mixed_slot_seconds": difference,
            "whole_second_duration_difference_bound_seconds": bound,
            "net_difference_direction_survives_quantization": abs(difference) > bound,
            "physical_gpu_cost_reduction_established": False,
            "energy_cost": None,
            "main_jobs_per_policy": 9 * plan["jobs_per_cohort"],
            "interpretation": "finite main-job policy budget; shared slot units cannot establish physical GPU cost or extrapolated payback",
        }
    return summary


def _sensor_windows(job):
    points = job["details"].get("sensor", [])
    integral = observed = 0.0
    for a, b in zip(points, points[1:], strict=False):
        dt = b["at"] - a["at"]
        if (
            0 < dt <= 1
            and a["source"] == b["source"]
            and all(
                _finite(p.get("utilization"))
                and 0 <= p["utilization"] <= 100
                and not p.get("error")
                for p in (a, b)
            )
        ):
            integral += dt * (a["utilization"] + b["utilization"]) / 2
            observed += dt
    return {
        "attempt_id": job["attempt_id"],
        "candidate": job["candidate_ref"],
        "sources": sorted({p["source"] for p in points}),
        "samples": len(points),
        "sampled_load_percent": integral / observed if observed else None,
        "observed_seconds": observed,
        "per_job_attribution_established": False,
        "scope": "physical device load in sampled execution windows; source-specific, overlap/interference retained",
    }


def evaluate_gate(summary):
    """Apply every declared promotion condition; never infer evidence completeness.

    Quantization intervals are sensitivity bounds, not confidence intervals.
    All comparisons use complete cohorts. With n=3, passing remains descriptive.
    """
    checks = []

    def check(name, passed, **values):
        checks.append({"condition": name, "passed": bool(passed), **values})

    check("complete independently audited evidence", summary.get("evidence_complete") is True)
    ambiguities = summary.get("timing_ambiguities")
    check(
        "qualified endpoints, clocks and actual arrivals",
        ambiguities == [],
        ambiguities=ambiguities,
    )
    uncertainty = summary.get("native_jct_uncertainty_seconds")
    if not _finite(uncertainty) or uncertainty < 0:
        check("known native timestamp uncertainty", False)
        uncertainty = 0
    arrival_bound = summary.get("arrival_sensitivity_bound_seconds", 0)
    if not _finite(arrival_bound) or arrival_bound < 0:
        check("known actual-arrival sensitivity", False)
    else:
        uncertainty += arrival_bound
    cohort_index = {(c["load"], c["arm"], c["block"]): c for c in summary.get("cohorts", [])}
    expected = {(load, arm, block) for load in LOADS for arm in ARMS for block in range(3)}
    check(
        "all 27 independent cohort records",
        set(cohort_index) == expected and len(summary.get("cohorts", [])) == 27,
    )
    try:
        loads = summary["loads"]
        for comparator in ARMS[:2]:
            for load in ("moderate", "burst"):
                for block in range(3):
                    control = cohort_index[(load, comparator, block)]
                    queue = cohort_index[(load, "profile_queue", block)]
                    a, b = control["mean_jct_seconds"], queue["mean_jct_seconds"]
                    check(
                        f"{comparator}: {load} block {block} lower mean JCT",
                        b < a,
                        control_seconds=a,
                        queue_seconds=b,
                    )
                    check(
                        f"{comparator}: {load} block {block} lower mean survives timestamps",
                        b + uncertainty < a - uncertainty,
                        timestamp_duration_bound_seconds=uncertainty,
                    )
                    check(
                        f"{comparator}: {load} block {block} compute-end corroboration",
                        queue["mean_acceptance_to_compute_end_seconds"]
                        < control["mean_acceptance_to_compute_end_seconds"],
                    )
            a_rows = [loads[load][comparator] for load in ("moderate", "burst")]
            b_rows = [loads[load]["profile_queue"] for load in ("moderate", "burst")]
            a = sum(r["mean_jct_seconds"] * r["jobs"] for r in a_rows) / sum(
                r["jobs"] for r in a_rows
            )
            b = sum(r["mean_jct_seconds"] * r["jobs"] for r in b_rows) / sum(
                r["jobs"] for r in b_rows
            )
            improvement = (1 - b / a) * 100
            robust_improvement = (
                (1 - (b + uncertainty) / (a - uncertainty)) * 100 if a > uncertainty else None
            )
            check(
                f"{comparator}: aggregate moderate/burst mean improves at least 10%",
                improvement >= 10,
                observed_improvement_percent=improvement,
            )
            check(
                f"{comparator}: 10% improvement survives timestamps",
                robust_improvement is not None and robust_improvement >= 10,
                worst_case_improvement_percent=robust_improvement,
            )
            for load in LOADS:
                control, queue = loads[load][comparator], loads[load]["profile_queue"]
                check(
                    f"{comparator}: {load} p95 regression at most 5%",
                    queue["p95_jct_seconds"] <= control["p95_jct_seconds"] * 1.05,
                )
                check(
                    f"{comparator}: {load} p95 bound at most 5%",
                    queue["p95_jct_seconds"] + uncertainty
                    <= (control["p95_jct_seconds"] - uncertainty) * 1.05,
                )
                check(
                    f"{comparator}: {load} throughput regression at most 5%",
                    queue["native_throughput_jobs_per_second"]
                    >= control["native_throughput_jobs_per_second"] * 0.95,
                )
                control_wall_lower = control["native_wall_seconds"] - uncertainty * 3
                queue_wall_upper = queue["native_wall_seconds"] + uncertainty * 3
                check(
                    f"{comparator}: {load} throughput bound at most 5%",
                    control_wall_lower > 0
                    and queue_wall_upper > 0
                    and control_wall_lower / queue_wall_upper >= 0.95,
                )
            control, queue = loads["sparse"][comparator], loads["sparse"]["profile_queue"]
            check(
                f"{comparator}: sparse mean regression at most 5%",
                queue["mean_jct_seconds"] <= control["mean_jct_seconds"] * 1.05,
            )
            check(
                f"{comparator}: sparse mean bound at most 5%",
                queue["mean_jct_seconds"] + uncertainty
                <= (control["mean_jct_seconds"] - uncertainty) * 1.05,
            )
    except (KeyError, TypeError, ZeroDivisionError) as exc:
        check("complete comparable promotion metrics", False, failure=str(exc))
    reasons = [c["condition"] for c in checks if not c["passed"]]
    return {
        "passed": not reasons,
        "reasons": reasons,
        "checks": checks,
        "statistical_significance_established": False,
        "production_integration_authorized_by_evidence": not reasons,
    }


def audit_capture(directory):
    """Resolve and audit an archive without submitting or importing any workload.

    Input hashes describe the exact bytes consumed. Missing timing qualification
    leaves native descriptive metrics available but blocks promotion.
    """
    directory = Path(directory).resolve()
    hashes = {}

    def read(name, expected_hash=None):
        relative = Path(name)
        _require(not relative.is_absolute() and ".." not in relative.parts, "archive path escape")
        path = (directory / relative).resolve()
        _require(
            path.is_relative_to(directory) and path.is_file(),
            "missing archive input or path escape",
        )
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        _require(expected_hash is None or digest == expected_hash, "archive input hash mismatch")
        hashes[relative.as_posix()] = digest
        return raw

    def read_json(name, expected_hash=None):
        try:
            return json.loads(read(name, expected_hash))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("malformed archive JSON") from exc

    capture = read_json("capture.json")
    plan = capture["plan"]
    _require(plan == read_json("plan.json"), "changed independently archived plan")
    source = read_json("source.json")
    _require(source["immutable"] is True, "mutable workload source")
    fixture_raw, workload_raw = (
        source["data"][k].encode() for k in ("fixture.json", "workload.py")
    )
    for raw, key in ((fixture_raw, "fixture_sha256"), (workload_raw, "source_sha256")):
        _require(
            hashlib.sha256(raw).hexdigest() == plan[key] == FROZEN_WORKLOAD[key],
            "changed frozen workload source",
        )
    try:
        tree = ast.parse(workload_raw)
        ptx = next(
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "PTX" for target in node.targets)
        )
    except (SyntaxError, ValueError, StopIteration) as exc:
        raise ValueError("missing frozen PTX") from exc
    _require(
        hashlib.sha256(ptx).hexdigest()
        == plan["kernel_sha256"]
        == FROZEN_WORKLOAD["kernel_sha256"],
        "changed fixed CUDA kernel",
    )
    fixture = json.loads(fixture_raw)
    _require(
        fixture["batch_size"] == 256
        and fixture["accuracy"] == 0.98046875
        and fixture["precision"] == "fp32",
        "wrong fixed fixture",
    )
    for cohort in capture["cohorts"]:
        resolved = []
        for entry in cohort["jobs"]:
            if "job_file" in entry:
                _require(bool(entry.get("job_sha256")), "unhashed external job")
                entry = read_json(entry["job_file"], entry["job_sha256"])
            resolved.append(entry)
        cohort["jobs"] = resolved
    profiles = capture["qualifications"]
    all_jobs = [*profiles, *(j for c in capture["cohorts"] for j in c["jobs"])]
    for job in all_jobs:
        receipt = job["native_receipt"]
        filename = receipt.get("log_file")
        if filename is None:
            options = (job["attempt_id"] + ".log", "logs/" + job["attempt_id"] + ".log")
            filename = next((f for f in options if (directory / f).is_file()), None)
        _require(filename is not None, "missing native workload log")
        raw = read(filename, receipt["log_sha256"])
        lines = [
            line[len("POOL_INFERENCE_RESULT ") :]
            for line in raw.decode().splitlines()
            if line.startswith("POOL_INFERENCE_RESULT ")
        ]
        _require(len(lines) == 1, "missing or duplicate native result line")
        _require(json.loads(lines[0]) == receipt["result"], "changed raw native log result")
        if receipt.get("native_snapshot_ref"):
            snapshot = read_json(receipt["native_snapshot_ref"], receipt["native_snapshot_sha256"])
            _validate_terminal_snapshot(snapshot, receipt, job)
    summary = summarize_capture(capture)
    summary["archive_integrity_verified"] = True
    summary["round_level_compute_evidence_complete"] = all(
        "round_seconds" in j["details"] for j in all_jobs
    )
    main_jobs = [j for c in capture["cohorts"] for j in c["jobs"]]
    summary["native_terminal_snapshots_verified"] = sum(
        bool(j["native_receipt"].get("native_snapshot_ref")) for j in main_jobs
    )
    ambiguities = [
        "clock alignment unqualified",
        "actual arrival comparability unqualified",
        "pre-outcome decision provenance unqualified",
    ]
    summary["timing_ambiguities"] = ambiguities
    summary["native_jct_uncertainty_seconds"] = 1
    arrivals = _arrival_evidence(capture)
    summary.update(arrivals)
    if arrivals["arrivals_qualified"]:
        ambiguities.remove("actual arrival comparability unqualified")
    clock_proofs = {}
    for phase in ("before", "after"):
        filename = f"clock-alignment-{phase}-qualified.json"
        if (directory / filename).exists():
            clock_proofs[phase] = _validate_clock_proof(read_json(filename), capture, phase)
    clocks_qualified = len(clock_proofs) == 2
    summary["clock_alignment_qualified"] = clocks_qualified
    summary["clock_offset_bounds"] = clock_proofs
    if clocks_qualified:
        ambiguities.remove("clock alignment unqualified")
        largest_offset = max(
            abs(endpoint)
            for proof in clock_proofs.values()
            for bounds in proof.values()
            for endpoint in bounds
        )
        summary["native_jct_uncertainty_seconds"] += 2 * largest_offset
    decision_stats = _audit_decisions(capture, read_json, read)
    summary.update(decision_stats)
    if decision_stats["decisions_replayed"] == summary["expected_main_jobs"]:
        ambiguities.remove("pre-outcome decision provenance unqualified")
    if decision_stats["queue_fallback_jobs"]:
        ambiguities.append(
            "Queue policy abstained on incomplete state; fallback outcomes cannot qualify its complete pool"
        )
    if not decision_stats["selector_source_archived"]:
        ambiguities.append("committed selector source archive missing")
    if not summary["round_level_compute_evidence_complete"]:
        ambiguities.append("per-round synchronized compute evidence missing")
    if summary["native_terminal_snapshots_verified"] != summary["expected_main_jobs"]:
        ambiguities.append("raw terminal native snapshot provenance incomplete")
    if decision_stats["queue_scope_snapshots_verified"] != summary["expected_main_jobs"]:
        ambiguities.append("native queue observation scope incomplete")
    schedule_name = "preregistered-schedule.json"
    if (directory / schedule_name).exists():
        schedule = read_json(schedule_name, FROZEN_SCHEDULES[plan["jobs_per_cohort"]])
        actual_order = [(c["load"], c["block"], c["arm"]) for c in capture["cohorts"]]
        planned_order = [(c["load"], c["block"], c["arm"]) for c in schedule["cohorts"]]
        _require(actual_order == planned_order, "changed preregistered cohort schedule")
        _require(
            all(
                c["jobs"] == plan["jobs_per_cohort"]
                and c["arrival_interval_seconds"] == LOADS[c["load"]]
                for c in schedule["cohorts"]
            ),
            "changed preregistered schedule work/arrivals",
        )
        summary["cohort_schedule_verified"] = True
    else:
        summary["cohort_schedule_verified"] = False
        ambiguities.append("independent preregistered cohort schedule missing")
    summary["endpoint_limitation"] = (
        "K8s container exit and Slurm allocation EndTime differ; common compute-end durations "
        "use worker wall clocks and require clock alignment. Native durations have whole-second resolution."
    )
    rows = []
    for cohort in capture["cohorts"]:
        for job in cohort["jobs"]:
            detail, receipt = job["details"], job["native_receipt"]
            container_start = (
                None if job["backend"] == "slurm" else _epoch(receipt["termination"]["startedAt"])
            )
            rows.append(
                {
                    "load": cohort["load"],
                    "arm": cohort["arm"],
                    "block": cohort["block"],
                    "attempt_id": job["attempt_id"],
                    "native_id": job["native_id"],
                    "candidate": job["candidate_ref"],
                    "backend": job["backend"],
                    "jct_seconds": job["finished_at"] - job["accepted_at"],
                    "wait_seconds": job["started_at"] - job["accepted_at"],
                    "request_to_native_end_seconds": job["finished_at"] - job["request_started_at"],
                    "acceptance_to_compute_end_seconds": detail["compute_finished_at"]
                    - job["accepted_at"],
                    "scheduled_to_compute_start_seconds": detail["compute_started_at"]
                    - job["started_at"],
                    "scheduled_to_container_start_seconds": (
                        container_start - job["started_at"] if container_start is not None else None
                    ),
                    "signed_postcompute_to_native_end_seconds": job["finished_at"]
                    - detail["compute_finished_at"],
                    "synchronized_inference_seconds": detail["elapsed_seconds"],
                    "accelerator_slot_seconds": job["gpu_seconds"],
                    "reservation_unit": _reservation_key(job, plan),
                    "images": job["units"],
                    "quality": job["quality"],
                    "accuracy": detail["accuracy"],
                    "request_started_at": job["request_started_at"],
                    "accepted_at": job["accepted_at"],
                    "scheduled_at": job["started_at"],
                    "native_finished_at": job["finished_at"],
                    "planned_arrival_offset": job.get("planned_arrival_offset"),
                    "clock_alignment_qualified": clocks_qualified,
                }
            )
    summary["evidence_complete"] = (
        decision_stats["decisions_replayed"] == summary["expected_main_jobs"]
        and decision_stats["selector_source_archived"]
        and summary["cohort_schedule_verified"]
    )
    summary["gate"] = evaluate_gate(summary)
    return {
        "summary": summary,
        "main_rows": rows,
        "cohort_rows": summary["cohorts"],
        "input_hashes": dict(sorted(hashes.items())),
    }


def _audit_decisions(capture, read_json, read):
    """Replay the public committed selector from immutable, pre-outcome inputs."""
    from dataclasses import asdict

    plan = capture["plan"]
    if plan.get("policy_sha256"):
        policy_raw = read("jct_selection.py", plan["policy_sha256"])
        _require(
            policy_raw == Path(jct_selection.__file__).read_bytes(),
            "auditor selector version differs from archived committed policy",
        )
    identities = [
        jct_selection.CandidateIdentity(
            n["ref"],
            n["backend"],
            "local-native-pool",
            n["node"],
            n["resource_key"],
            n.get("image", "slurm-python3-cuda-driver") + ":" + n.get("runtime_class", "default"),
            plan["source_sha256"] + ":" + plan["fixture_sha256"],
        )
        for n in plan["pool"]
    ]
    identity_map = {i.candidate_ref: i for i in identities}
    qualified = {j["attempt_id"]: j for j in capture["qualifications"]}
    profile_files = {}
    if capture.get("calibration_source"):
        source = capture["calibration_source"]
        calibration = read_json(source["file"], source["sha256"])
        _require(
            calibration["qualifications"] == capture["qualifications"],
            "changed frozen calibration evidence",
        )
    else:
        calibration = capture
    replayed = fallbacks = complete_queue = scope_verified = 0
    for cohort in capture["cohorts"]:
        for index, job in enumerate(cohort["jobs"]):
            evidence = job.get("choice_evidence")
            if evidence is None:
                continue
            choice = (
                read_json(evidence["choice_file"], evidence["choice_sha256"])
                if "choice_file" in evidence
                else evidence
            )
            start, selected_at, end = (
                choice[k] for k in ("decision_started_at", "selection_at", "decision_finished_at")
            )
            _require(
                all(_finite(t) for t in (start, selected_at, end))
                and start <= selected_at <= end <= job["request_started_at"],
                "choice lacks pre-outcome decision boundaries",
            )
            _require(
                choice["policy"] == cohort["arm"]
                and choice["candidate_ref"] == job["candidate_ref"]
                and choice["rr_index"] == index,
                "choice policy/candidate identity mismatch",
            )
            snapshot = read_json(choice["snapshot_ref"], choice["snapshot_sha256"])
            _require(
                snapshot["observed_at"] <= snapshot["finished_at"] <= selected_at,
                "future or unresolved queue observation",
            )
            scope_verified += _validate_queue_scope(snapshot, choice["queues"])
            profiles = []
            for raw in choice["profiles"]:
                identity = jct_selection.CandidateIdentity(**raw["identity"])
                _require(
                    identity_map.get(identity.candidate_ref) == identity,
                    "profile route identity mismatch",
                )
                ref = identity.candidate_ref
                if ref not in profile_files:
                    saved = read_json("profiles/" + ref + ".json")
                    profile_files[ref] = saved
                    _validate_frozen_profile(saved, qualified, calibration, plan, ref)
                saved = profile_files[ref]
                _require(
                    saved["estimated_profile"] == raw, "profile changed after calibration freeze"
                )
                profiles.append(
                    jct_selection.MeasuredProfile(
                        **{**raw, "identity": identity, "timing_notes": tuple(raw["timing_notes"])}
                    )
                )
            refs = choice["profile_source_refs"]
            _require(
                all(
                    ref in qualified and qualified[ref]["candidate_ref"] == job["candidate_ref"]
                    for ref in refs
                ),
                "future or foreign profile source",
            )
            if cohort["arm"] == "round_robin":
                _require(not refs and not profiles, "Round Robin consumed profiles")
            else:
                _require(
                    len(profiles) == 6
                    and set(refs)
                    == {p["attempt_id"] for p in profile_files[job["candidate_ref"]]["raw_phases"]},
                    "incomplete frozen profile sources",
                )
            queues = []
            for raw in choice["queues"]:
                identity = jct_selection.CandidateIdentity(**raw["identity"])
                _require(
                    identity_map.get(identity.candidate_ref) == identity
                    and raw["snapshot_ref"] == choice["snapshot_ref"]
                    and raw["observed_at"] == snapshot["observed_at"],
                    "queue route/snapshot identity mismatch",
                )
                node = next(n for n in plan["pool"] if n["ref"] == identity.candidate_ref)
                _require(raw["capacity"] == node["nominal_slots"], "changed native queue capacity")
                queues.append(
                    jct_selection.QueueSnapshot(
                        **{
                            **raw,
                            "identity": identity,
                            "running": tuple(jct_selection.WorkItem(**j) for j in raw["running"]),
                            "pending": tuple(jct_selection.WorkItem(**j) for j in raw["pending"]),
                            "observed_job_ids": tuple(raw["observed_job_ids"]),
                        }
                    )
                )
            intents = []
            for raw in choice["intents"]:
                identity = jct_selection.CandidateIdentity(**raw["identity"])
                _require(
                    identity_map.get(identity.candidate_ref) == identity
                    and raw["submitted_at"] <= selected_at
                    and raw["job_id"] != job["attempt_id"],
                    "future or foreign submit intent",
                )
                intents.append(jct_selection.SubmitIntent(**{**raw, "identity": identity}))
            actual = jct_selection.select_candidate(
                choice["policy"],
                identities,
                profiles,
                now_seconds=selected_at,
                queues=queues,
                intents=intents,
                rr_index=index,
                max_queue_age_seconds=choice.get("max_queue_age_seconds", 5.0),
                max_profile_age_seconds=choice.get("max_profile_age_seconds", 86400.0),
                cohort_backlog_seconds=choice["cohort_backlog_seconds"],
            )
            _require(
                json.loads(json.dumps(asdict(actual))) == choice["decision"],
                "committed policy decision replay mismatch",
            )
            if actual.candidate_ref is None:
                fallback = jct_selection.select_candidate(
                    "round_robin", identities, [], now_seconds=selected_at, rr_index=index
                )
                _require(
                    json.loads(json.dumps(asdict(fallback))) == choice["fallback"]
                    and fallback.candidate_ref == job["candidate_ref"],
                    "unqualified or changed explicit fallback",
                )
                if cohort["arm"] == "profile_queue":
                    fallbacks += 1
            else:
                _require(
                    choice["fallback"] is None and actual.candidate_ref == job["candidate_ref"],
                    "changed selected candidate or unexplained fallback",
                )
                if cohort["arm"] == "profile_queue":
                    complete_queue += 1
            replayed += 1
    return {
        "decisions_replayed": replayed,
        "queue_fallback_jobs": fallbacks,
        "queue_policy_selected_jobs": complete_queue,
        "selector_source_archived": bool(plan.get("policy_sha256")),
        "queue_scope_snapshots_verified": scope_verified,
    }


def _validate_frozen_profile(saved, qualified, calibration, plan, ref):
    node = next(n for n in plan["pool"] if n["ref"] == ref)
    jobs = [j for j in qualified.values() if j["candidate_ref"] == ref]
    _require(len(jobs) == node["nominal_slots"] + 1, "missing capacity+1 full-work calibration")
    phases = [
        {
            "attempt_id": j["attempt_id"],
            "compute": j["details"]["elapsed_seconds"],
            "preparation": j["details"]["compute_started_at"] - j["started_at"],
            "release": j["finished_at"] - j["details"]["compute_finished_at"],
            "native_occupancy": j["gpu_seconds"],
            "admission": j["started_at"] - j["accepted_at"],
        }
        for j in jobs
    ]
    _require(phases == saved["raw_phases"], "fabricated calibration phase costs")
    prior = sorted(jobs, key=lambda j: (j["started_at"], j["attempt_id"]))[: node["nominal_slots"]]
    follower = sorted(jobs, key=lambda j: (j["started_at"], j["attempt_id"]))[node["nominal_slots"]]
    release = min(j["finished_at"] for j in prior)
    reentry = (
        follower["started_at"] - release
        if max(j["started_at"] for j in prior) < release
        and follower["accepted_at"] < release <= follower["started_at"]
        else None
    )
    _require(
        saved["readmission_seconds"] == reentry
        and saved["readmission_predecessors"] == [j["attempt_id"] for j in prior]
        and saved["readmission_follower"] == follower["attempt_id"],
        "unobserved or fabricated native reentry",
    )
    profile = saved["estimated_profile"]
    expected = {
        "compute_seconds": statistics.median(p["compute"] for p in phases),
        "preparation_seconds": max(
            0.0, float(round(statistics.median(p["preparation"] for p in phases)))
        ),
        "release_seconds": max(0.0, float(round(statistics.median(p["release"] for p in phases)))),
        "admission_seconds": statistics.median(j["started_at"] - j["accepted_at"] for j in prior),
        "v2_service_seconds": statistics.median(p["native_occupancy"] for p in phases),
        "readmission_seconds": reentry,
        "measured_at": calibration["calibration_completed_at"],
    }
    _require(
        all(profile[k] == value for k, value in expected.items()),
        "profile estimate differs from frozen observed rule",
    )
    _require(
        profile["qualified"] is True
        and profile["quality_passed"] is True
        and profile["synthetic"] is False,
        "unqualified frozen profile",
    )


def _validate_clock_proof(proof, capture, phase):
    expected = {n["ref"] for n in capture["plan"]["pool"]} | {"slurm-controller"}
    observations = proof["observations"]
    _require(
        {o["candidate"] for o in observations} == expected and len(observations) == 7,
        "missing or duplicate clock endpoints",
    )
    main_jobs = [j for c in capture["cohorts"] for j in c["jobs"]]
    first = min(j["request_started_at"] for j in main_jobs)
    last = max(j.get("observed_complete_at", j["finished_at"]) for j in main_jobs)
    result = {}
    for observation in observations:
        samples = observation["samples"]
        _require(len(samples) >= 3, "insufficient clock probes")
        for sample in samples:
            a, b, remote = (sample[k] for k in ("local_before", "local_after", "remote_epoch"))
            _require(
                all(_finite(v) for v in (a, b, remote)) and a <= b,
                "invalid clock round-trip boundaries",
            )
            expected_bounds = [remote - b, remote - a]
            _require(
                all(
                    math.isclose(x, y, abs_tol=1e-7, rel_tol=0)
                    for x, y in zip(sample["offset_bounds"], expected_bounds, strict=True)
                )
                and math.isclose(sample["rtt_seconds"], b - a, abs_tol=1e-7, rel_tol=0),
                "fabricated clock offset or RTT",
            )
            _require(
                b < first if phase == "before" else a > last,
                "clock proof does not bracket main work",
            )
        best = min(samples, key=lambda s: s["rtt_seconds"])
        _require(observation["best"] == best, "clock qualification did not use measured best RTT")
        _require(
            max(abs(v) for v in best["offset_bounds"]) <= 1,
            "clock offset exceeds declared one-second qualification",
        )
        result[observation["candidate"]] = best["offset_bounds"]
    return result


def _arrival_evidence(capture):
    rows, traces = [], {}
    qualified = True
    for cohort in capture["cohorts"]:
        jobs = cohort["jobs"]
        interval = LOADS[cohort["load"]]
        _require(
            all(j.get("planned_arrival_offset") == i * interval for i, j in enumerate(jobs)),
            "changed preregistered arrival trace",
        )
        first_request = jobs[0]["request_started_at"]
        trace = [j["request_started_at"] - first_request for j in jobs]
        deviations = [abs(value - i * interval) for i, value in enumerate(trace)]
        native_spread = max(j["accepted_at"] for j in jobs) - min(j["accepted_at"] for j in jobs)
        lateness = [j.get("decision_lateness_seconds") for j in jobs]
        if not all(_finite(x) and x >= 0 for x in lateness):
            qualified = False
        planned_monotonic = [j.get("planned_arrival_monotonic") for j in jobs]
        if all(_finite(x) for x in planned_monotonic):
            _require(
                all(
                    math.isclose(t - planned_monotonic[0], i * interval, abs_tol=1e-6, rel_tol=0)
                    for i, t in enumerate(planned_monotonic)
                ),
                "changed monotonic arrival deadlines",
            )
        else:
            qualified = False
        for job in jobs:
            _require(
                job["request_started_at"] <= job["submit_response_at"], "reversed submission clock"
            )
            if not _finite(job.get("observed_complete_at")) or not _finite(
                job.get("intent_created_at")
            ):
                qualified = False
            else:
                _require(
                    job["intent_created_at"] <= job["request_started_at"]
                    and job["submit_response_at"] <= job["observed_complete_at"],
                    "reversed arrival/observation clock",
                )
        traces[(cohort["load"], cohort["block"], cohort["arm"])] = trace
        rows.append(
            {
                "load": cohort["load"],
                "arm": cohort["arm"],
                "block": cohort["block"],
                "intended_interval_seconds": interval,
                "intended_spread_seconds": interval * (len(jobs) - 1),
                "actual_request_spread_seconds": max(trace) - min(trace),
                "actual_native_acceptance_spread_seconds": native_spread,
                "request_trace_deviation_seconds": max(deviations),
                "mean_decision_lateness_seconds": statistics.mean(lateness)
                if all(_finite(x) for x in lateness)
                else None,
                "max_decision_lateness_seconds": max(lateness)
                if all(_finite(x) for x in lateness)
                else None,
                "mean_submit_round_trip_seconds": statistics.mean(
                    j["submit_response_at"] - j["request_started_at"] for j in jobs
                ),
                "normalized_request_offsets": trace,
            }
        )
    paired_bound = max(
        abs(a - b)
        for load in LOADS
        for block in range(3)
        for comparator in ARMS[:2]
        for a, b in zip(
            traces[(load, block, comparator)], traces[(load, block, "profile_queue")], strict=True
        )
    )
    return {
        "arrival_evidence": rows,
        "arrivals_qualified": qualified,
        "arrival_sensitivity_bound_seconds": paired_bound,
        "arrival_bound_interpretation": "largest paired difference in normalized client request trace; conservative timestamp sensitivity, not a causal queueing-model confidence bound",
    }


def _validate_terminal_snapshot(snapshot, receipt, job):
    if job["backend"] == "kubernetes":
        raw_items = snapshot["raw"]["kubernetes"]["items"]
        native_jobs = [
            item
            for item in raw_items
            if item["kind"] == "Job"
            and item["metadata"]["uid"] == receipt["job"]["metadata"]["uid"]
        ]
        native_pods = [
            item
            for item in raw_items
            if item["kind"] == "Pod"
            and item["metadata"]["uid"] == receipt["pod"]["metadata"]["uid"]
        ]
        _require(
            native_jobs == [receipt["job"]] and native_pods == [receipt["pod"]],
            "changed raw native Kubernetes terminal receipt",
        )
    else:
        response = snapshot["raw"]["slurm"]["accounting"]
        _require(response["exit"] == 0, "failed raw Slurm accounting query")
        keys = (
            "native_id",
            "name",
            "state",
            "exit_code",
            "submit",
            "start",
            "end",
            "elapsed_raw",
            "req_tres",
            "alloc_tres",
            "nodes",
            "account",
            "qos",
        )
        rows = []
        for line in response["stdout"].splitlines():
            if not line.strip():
                continue
            fields = [field.strip() for field in line.split("|")]
            _require(len(fields) == len(keys), "malformed raw native accounting row")
            if fields[0] == str(job["native_id"]):
                rows.append(dict(zip(keys, fields, strict=True)))
        _require(len(rows) == 1, "ambiguous or missing raw Slurm primary allocation")
        native = receipt["native_accounting"]
        _require(
            all(native[key] == rows[0][key] for key in keys), "changed raw Slurm primary allocation"
        )


def _validate_queue_scope(snapshot, queues):
    raw = snapshot.get("raw", {}).get("kubernetes")
    if not raw or "native_lists" not in raw:
        return False
    native_lists = raw["native_lists"]
    items = native_lists["jobs_pods"]["items"] + native_lists["workloads"]["items"]
    _require(raw["items"] == items, "changed native Workload UID query result")
    for key, kind in (("jobs", "Job"), ("pods", "Pod"), ("workloads", "Workload")):
        _require(
            snapshot[key] == [item for item in items if item["kind"] == kind],
            "changed normalized native queue observation",
        )
    for queue in queues:
        if not queue["complete"]:
            continue
        _require(not snapshot.get("errors"), "complete queue claims failed native observations")
        if queue["identity"]["backend"] != "kubernetes":
            continue
        node = queue["identity"]["node_ref"]
        for job in snapshot["jobs"]:
            spec = job["spec"]["template"]["spec"]
            if spec.get("nodeSelector", {}).get("kubernetes.io/hostname") != node:
                continue
            if job.get("status", {}).get("succeeded") or job.get("status", {}).get("failed"):
                continue
            owner = job["metadata"]
            workloads = [
                w
                for w in snapshot["workloads"]
                if any(
                    o.get("kind") == "Job"
                    and o.get("uid") == owner["uid"]
                    and o.get("name") == owner["name"]
                    for o in w["metadata"].get("ownerReferences", [])
                )
            ]
            _require(len(workloads) == 1, "complete queue omitted or duplicated own Workload")
    return True
