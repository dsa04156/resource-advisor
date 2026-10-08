"""Strict native evidence normalization for the experimental JCT dispatcher."""

import hashlib
import json
import math
from datetime import datetime

TERMINAL = {
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "TIMEOUT",
    "NODE_FAIL",
    "OUT_OF_MEMORY",
    "BOOT_FAIL",
    "DEADLINE",
}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def parse_native_time(value):
    """Accept an explicit epoch or aware ISO date; never guess a local timezone."""
    value = str(value).strip()
    if value in {"", "Unknown", "N/A", "None", "0"}:
        return None
    if value.isdigit():
        return float(value)
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid native timestamp") from exc
    if date.tzinfo is None or date.utcoffset() is None:
        raise ValueError("native timestamp must include timezone")
    result = date.timestamp()
    if not math.isfinite(result):
        raise ValueError("invalid native timestamp")
    return result


def parse_squeue(raw):
    rows = []
    keys = (
        "native_id",
        "name",
        "account",
        "partition",
        "qos",
        "state",
        "nodes",
        "requested_nodes",
        "start",
        "submit",
        "reason",
    )
    for line in raw.splitlines():
        if not line.strip():
            continue
        values = [value.strip() for value in line.split("|")]
        if len(values) != len(keys):
            raise ValueError("malformed squeue row")
        row = dict(zip(keys, values, strict=True))
        start = parse_native_time(row["start"])
        row["started_at"] = start if row["state"] in {"RUNNING", "COMPLETING"} else None
        row["expected_start_at"] = start if row["state"] == "PENDING" else None
        row["accepted_at"] = parse_native_time(row["submit"])
        rows.append(row)
    return rows


def parse_sacct(raw):
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
    for line in raw.splitlines():
        if not line.strip():
            continue
        values = [value.strip() for value in line.split("|")]
        if len(values) != len(keys):
            raise ValueError("malformed sacct row")
        row = dict(zip(keys, values, strict=True))
        row.update(
            accepted_at=parse_native_time(row["submit"]),
            started_at=parse_native_time(row["start"]),
            finished_at=parse_native_time(row["end"]),
        )
        rows.append(row)
    return rows


def result_line(log):
    lines = [
        line.split(" ", 1)[1]
        for line in log.splitlines()
        if line.startswith("POOL_INFERENCE_RESULT ")
    ]
    if len(lines) != 1:
        raise ValueError("exactly one workload result is required")
    return json.loads(lines[0])


def validate_result(result, plan, rounds):
    if result.get("measured") is not True or result.get("outcome") != "COMPLETED":
        raise ValueError("workload did not complete measured work")
    for key in ("fixture_sha256", "kernel_sha256", "accuracy"):
        if result.get(key) != plan.get(key):
            raise ValueError("workload identity/quality mismatch: " + key)
    if (
        result.get("rounds") != rounds
        or result.get("images") != rounds * 256
        or result.get("quality") != 1
    ):
        raise ValueError("wrong fixed work or quality")
    seconds = result.get("round_seconds", [])
    if len(seconds) != rounds or any(not math.isfinite(x) or x < 0 for x in seconds):
        raise ValueError("incomplete measured rounds")
    if abs(sum(seconds) - result["elapsed_seconds"]) > 1e-9:
        raise ValueError("compute sum mismatch")


def validate_boundaries(accepted, started, finished):
    if any(value is None or not math.isfinite(value) for value in (accepted, started, finished)):
        raise ValueError("native boundary unknown")
    if not accepted <= started <= finished:
        raise ValueError("native boundaries reversed")


def normalize_slurm(rows, item, log, plan):
    matches = [row for row in rows if row["native_id"] == item["native_id"]]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("ambiguous primary accounting identity")
    row = matches[0]
    if (
        row["name"] != item["attempt_id"]
        or row["account"] != "ra-lab"
        or row["qos"] != "ra-normal"
        or row["nodes"] != "slurm-w2"
    ):
        raise ValueError("native Slurm identity mismatch")
    state = row["state"].split()[0].split("+")[0]
    if state not in TERMINAL:
        return None
    validate_boundaries(row["accepted_at"], row["started_at"], row["finished_at"])
    result = result_line(log) if state == "COMPLETED" and row["exit_code"] == "0:0" else None
    if result is not None:
        validate_result(result, plan, item["requested_work"])
    return dict(
        attempt_id=item["attempt_id"],
        native_id=item["native_id"],
        native_state=state,
        native_accounting=row,
        native_timezone="UTC",
        result=result,
        termination={"exitCode": 0 if state == "COMPLETED" and row["exit_code"] == "0:0" else 1},
        accepted_at=row["accepted_at"],
        scheduled_at=row["started_at"],
        finished_at=row["finished_at"],
        gpu_seconds=row["finished_at"] - row["started_at"],
        log_sha256=digest(log.encode()),
    )


def validate_kubernetes_identity(job, pod, item, node, experiment):
    meta = job["metadata"]
    if (
        meta["name"] != item["attempt_id"]
        or meta.get("labels", {}).get("resource-advisor/experiment") != experiment
    ):
        raise ValueError("native Job identity mismatch")
    owners = pod["metadata"].get("ownerReferences", [])
    if not any(owner.get("uid") == meta["uid"] and owner.get("kind") == "Job" for owner in owners):
        raise ValueError("foreign Pod owner")
    spec = pod["spec"]
    container = spec["containers"][0]
    if (
        spec.get("nodeName") != node["node"]
        or spec.get("nodeSelector", {}).get("kubernetes.io/hostname") != node["node"]
    ):
        raise ValueError("wrong node route")
    if (
        container["image"] != node["image"]
        or container["resources"]["requests"].get(node["resource_key"]) != "1"
        or container["resources"]["limits"].get(node["resource_key"]) != "1"
    ):
        raise ValueError("wrong image/resource")
    if spec.get("runtimeClassName") != node.get("runtime_class"):
        raise ValueError("wrong runtime route")
    if (
        container["command"][-1] != str(item["requested_work"])
        or job["spec"].get("backoffLimit") != 0
        or job.get("status", {}).get("failed", 0)
    ):
        raise ValueError("wrong work/retry state")


def normalize_kubernetes(job, pods, item, node, experiment, log, plan):
    if not pods:
        return None
    if len(pods) != 1:
        raise ValueError("ambiguous native Pod count")
    pod = pods[0]
    states = pod.get("status", {}).get("containerStatuses", [])
    if len(states) != 1 or states[0].get("restartCount", 0):
        raise ValueError("container count/retry ambiguous")
    ended = states[0].get("state", {}).get("terminated")
    if ended is None:
        return None
    validate_kubernetes_identity(job, pod, item, node, experiment)
    scheduled = [
        c["lastTransitionTime"]
        for c in pod["status"].get("conditions", [])
        if c["type"] == "PodScheduled" and c["status"] == "True"
    ]
    if len(scheduled) != 1:
        raise ValueError("allocation start unknown")
    accepted, started, finished = (
        parse_native_time(value)
        for value in (job["metadata"]["creationTimestamp"], scheduled[0], ended["finishedAt"])
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
        native_state="COMPLETED" if ended["exitCode"] == 0 else "FAILED",
        accepted_at=accepted,
        scheduled_at=started,
        finished_at=finished,
        gpu_seconds=finished - started,
        log_sha256=digest(log.encode()),
    )


def balanced_schedule(seed):
    """Seed one arm permutation per load, then rotate once per paired repeat."""
    import random

    randomizer = random.Random(seed)
    result = []
    for load, interval in (("sparse", 3.0), ("moderate", 0.5), ("burst", 0.0)):
        arms = ["round_robin", "profile_only", "profile_queue"]
        randomizer.shuffle(arms)
        for block in range(3):
            for position, arm in enumerate(arms[block:] + arms[:block]):
                result.append(
                    dict(
                        load=load,
                        interval_seconds=interval,
                        block=block,
                        arm=arm,
                        order_in_block=position,
                    )
                )
    return result


def build_queues(snapshot, identities, profiles, pool, known, experiment):
    """Reconcile intent/native identities; unknown allocation stays unknown."""
    from resource_advisor.jct_selection import QueueSnapshot, WorkItem

    by_name = {item["attempt_id"]: item for item in known}
    by_id = {str(item["native_id"]): item for item in known if item.get("native_id")}
    profile_map = {p.identity.candidate_ref: p for p in profiles}
    nodes = {n["ref"]: n for n in pool}
    output = []
    for identity in identities:
        node = nodes[identity.candidate_ref]
        profile = profile_map[identity.candidate_ref]
        occupancy = profile.compute_seconds + profile.preparation_seconds + profile.release_seconds
        running, pending, observed = [], [], []
        complete = not snapshot.get("errors")
        if identity.backend == "kubernetes":
            for pod in snapshot.get("all_pods", []):
                spec = pod.get("spec", {})
                resource_requested = any(
                    c.get("resources", {}).get("requests", {}).get(identity.resource_key)
                    for c in spec.get("containers", [])
                )
                route = spec.get("nodeName") or spec.get("nodeSelector", {}).get(
                    "kubernetes.io/hostname"
                )
                if (
                    resource_requested
                    and (route == identity.node_ref or not route)
                    and pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
                ):
                    labels = pod.get("metadata", {}).get("labels", {})
                    if labels.get("resource-advisor/experiment") != experiment:
                        complete = False
            for job in snapshot.get("jobs", []):
                meta = job["metadata"]
                name = meta["name"]
                item = by_name.get(name)
                if not item:
                    spec = job.get("spec", {}).get("template", {}).get("spec", {})
                    route = spec.get("nodeSelector", {}).get("kubernetes.io/hostname")
                    claimed = any(
                        c.get("resources", {}).get("requests", {}).get(identity.resource_key)
                        for c in spec.get("containers", [])
                    )
                    terminal = bool(
                        job.get("status", {}).get("succeeded")
                        or job.get("status", {}).get("failed")
                    )
                    if claimed and (route == identity.node_ref or not route) and not terminal:
                        complete = False
                    continue
                if item["candidate_ref"] != identity.candidate_ref:
                    continue
                if meta.get("labels", {}).get("resource-advisor/experiment") != experiment:
                    complete = False
                    continue
                observed.append(name)
                uid = meta["uid"]
                pods = [
                    p
                    for p in snapshot.get("pods", [])
                    if any(
                        o.get("kind") == "Job" and o.get("uid") == uid
                        for o in p["metadata"].get("ownerReferences", [])
                    )
                ]
                workloads = [
                    w
                    for w in snapshot.get("workloads", [])
                    if any(
                        o.get("kind") == "Job" and o.get("uid") == uid
                        for o in w["metadata"].get("ownerReferences", [])
                    )
                ]
                if len(pods) > 1 or len(workloads) > 1:
                    complete = False
                    continue
                held = any(
                    any(
                        c.get("type") == "QuotaReserved" and c.get("status") == "True"
                        for c in w.get("status", {}).get("conditions", [])
                    )
                    and not any(
                        c.get("type") == "Finished" and c.get("status") == "True"
                        for c in w.get("status", {}).get("conditions", [])
                    )
                    for w in workloads
                )
                terminal = bool(
                    job.get("status", {}).get("succeeded") or job.get("status", {}).get("failed")
                )
                if terminal and not held:
                    continue
                if terminal and held:
                    pending.append(WorkItem(name, None, expected_admission_seconds=None))
                    continue
                pod = pods[0] if pods else None
                scheduled = [
                    c["lastTransitionTime"]
                    for c in (pod or {}).get("status", {}).get("conditions", [])
                    if c["type"] == "PodScheduled" and c["status"] == "True"
                ]
                states = (pod or {}).get("status", {}).get("containerStatuses", [])
                terminated = any(s.get("state", {}).get("terminated") for s in states)
                if terminated and held:
                    pending.append(WorkItem(name, None, expected_admission_seconds=None))
                elif scheduled:
                    if pod["spec"].get("nodeName") != identity.node_ref:
                        complete = False
                    running.append(
                        WorkItem(name, occupancy, started_at=parse_native_time(scheduled[0]))
                    )
                else:
                    pending.append(
                        WorkItem(
                            name,
                            occupancy,
                            expected_admission_seconds=item.get(
                                "expected_admission_seconds", profile.readmission_seconds
                            ),
                        )
                    )
        else:
            for row in snapshot.get("slurm_rows", []):
                item = by_id.get(row["native_id"])
                if (
                    row["nodes"] != identity.node_ref
                    and row["requested_nodes"] != identity.node_ref
                ):
                    continue
                if (
                    not item
                    or row["name"] != item["attempt_id"]
                    or row["account"] != "ra-lab"
                    or row["qos"] != "ra-normal"
                ):
                    complete = False
                    continue
                name = item["attempt_id"]
                observed.append(name)
                if row["state"] in {"RUNNING", "COMPLETING"}:
                    running.append(WorkItem(name, occupancy, started_at=row["started_at"]))
                elif row["state"] == "PENDING":
                    pending.append(
                        WorkItem(
                            name,
                            occupancy,
                            expected_admission_seconds=item.get(
                                "expected_admission_seconds", profile.readmission_seconds
                            ),
                        )
                    )
                else:
                    complete = False
            for row in snapshot.get("accounting_rows", []):
                item = by_id.get(row["native_id"])
                if (
                    item
                    and row["name"] == item["attempt_id"]
                    and row["account"] == "ra-lab"
                    and row["qos"] == "ra-normal"
                    and row["state"].split()[0].split("+")[0] in TERMINAL
                ):
                    observed.append(item["attempt_id"])
        # Already normalized terminal evidence also reconciles missing/deleted native records.
        observed.extend(
            item["attempt_id"]
            for item in known
            if item["candidate_ref"] == identity.candidate_ref and item.get("outcome")
        )
        output.append(
            QueueSnapshot(
                identity,
                snapshot["snapshot_ref"],
                snapshot["observed_at"],
                node["nominal_slots"],
                complete,
                tuple(running),
                tuple(pending),
                "logical_slot" if node["nominal_slots"] > 1 else "physical_device",
                tuple(sorted(set(observed))),
            )
        )
    return tuple(output)


def freeze_profiles(calibration, cluster_ref="local-native-pool"):
    """Freeze quantized estimates once; preserve raw cross-clock phases separately."""
    from statistics import median

    from resource_advisor.jct_selection import CandidateIdentity, MeasuredProfile

    plan = calibration["plan"]
    identities, profiles, evidence = [], [], {}
    for node in plan["pool"]:
        identity = CandidateIdentity(
            node["ref"],
            node["backend"],
            cluster_ref,
            node["node"],
            node["resource_key"],
            node.get("image", "slurm-python3-cuda-driver")
            + ":"
            + node.get("runtime_class", "default"),
            plan["source_sha256"] + ":" + plan["fixture_sha256"],
        )
        jobs = [j for j in calibration["qualifications"] if j["candidate_ref"] == node["ref"]]
        if len(jobs) < node["nominal_slots"] + 1 or any(
            j.get("outcome") != "SUCCEEDED" for j in jobs
        ):
            raise ValueError("missing fresh capacity+1 qualification")
        phases = []
        for job in jobs:
            result = job["details"]
            validate_result(result, plan, plan["main_rounds"])
            phases.append(
                dict(
                    attempt_id=job["attempt_id"],
                    compute=result["elapsed_seconds"],
                    preparation=result["compute_started_at"] - job["started_at"],
                    release=job["finished_at"] - result["compute_finished_at"],
                    native_occupancy=job["gpu_seconds"],
                    admission=job["started_at"] - job["accepted_at"],
                )
            )
        if any(p["preparation"] < -1 or p["release"] < -1 for p in phases):
            raise ValueError("profile phase exceeds one-second native timestamp uncertainty")
        ordered = sorted(jobs, key=lambda j: (j["started_at"], j["attempt_id"]))
        prior, follower = ordered[: node["nominal_slots"]], ordered[node["nominal_slots"]]
        first_release = min(j["finished_at"] for j in prior)
        reentry = None
        if (
            max(j["started_at"] for j in prior) < first_release
            and follower["accepted_at"] < first_release <= follower["started_at"]
        ):
            reentry = follower["started_at"] - first_release
        prep = max(0.0, float(round(median(p["preparation"] for p in phases))))
        release = max(0.0, float(round(median(p["release"] for p in phases))))
        admission = median(j["started_at"] - j["accepted_at"] for j in prior)
        profile = MeasuredProfile(
            identity,
            "profiles/" + node["ref"] + ".json",
            calibration["calibration_completed_at"],
            median(p["compute"] for p in phases),
            prep,
            admission,
            release,
            median(p["native_occupancy"] for p in phases),
            True,
            True,
            readmission_seconds=reentry,
            timing_uncertainty_seconds=1.0,
            timing_notes=(
                "native whole-second endpoints; round phase median to nearest second; negative within uncertainty maps to zero estimate, not measured zero",
                "worker/controller alignment must be separately qualified",
                "shared medians include all capacity+1 jobs; concurrent contention retained",
            ),
        )
        identities.append(identity)
        profiles.append(profile)
        evidence[node["ref"]] = dict(
            raw_phases=phases,
            readmission_seconds=reentry,
            readmission_predecessors=[j["attempt_id"] for j in prior],
            readmission_follower=follower["attempt_id"],
            estimated_profile=__import__("dataclasses").asdict(profile),
        )
    return tuple(identities), tuple(profiles), evidence
