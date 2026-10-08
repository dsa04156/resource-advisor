"""One bounded native Kueue cohort per arm using preserved CUDA microprofiles.

Experimental profile-aware dispatcher, not a change to production compile_plan.
The current Slurm controller outage remains an explicit exclusion.
"""

import argparse
import copy
import hashlib
import json
import random
import subprocess
import time
from datetime import datetime
from pathlib import Path

from analyze_pool_inference import audit


def epoch(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def kube(*args, structured=True):
    output = subprocess.check_output(
        ["rtk", "proxy", "kubectl", "--request-timeout=15s", "-n", "resource-advisor-lab", *args],
        text=True,
        timeout=25,
    )
    return json.loads(output) if structured else output


def receipt(name, directory):
    job = kube("get", "job", name, "-o", "json")
    pods = kube("get", "pods", "-l", "job-name=" + name, "-o", "json")["items"]
    if len(pods) != 1:
        return None
    pod = pods[0]
    statuses = pod.get("status", {}).get("containerStatuses", [])
    termination = statuses[0].get("state", {}).get("terminated") if statuses else None
    if not termination:
        return None
    scheduled = next(
        c["lastTransitionTime"]
        for c in pod["status"]["conditions"]
        if c["type"] == "PodScheduled" and c["status"] == "True"
    )
    log = kube("logs", "job/" + name, structured=False)
    (directory / (name + ".log")).write_text(log)
    result = next(
        (
            json.loads(line.split(" ", 1)[1])
            for line in log.splitlines()
            if line.startswith("POOL_INFERENCE_RESULT ")
        ),
        None,
    )
    return dict(
        attempt_id=name,
        job=job,
        pod=pod,
        result=result,
        termination=termination,
        scheduled_at=epoch(scheduled),
        finished_at=epoch(termination["finishedAt"]),
        gpu_seconds=epoch(termination["finishedAt"]) - epoch(scheduled),
        log_sha256=hashlib.sha256(log.encode()).hexdigest(),
    )


def main(directory):
    plan = json.loads((directory / "plan.json").read_text())
    path = directory / "capture.json"
    if path.exists():
        raise ValueError("Never replay an existing capture")
    deadline = time.monotonic() + 300
    state = dict(plan=plan, qualifications=[], cohorts=[], excluded_slurm=plan["Slurm"])
    for node in plan["pool"]:
        while True:
            observed = receipt(node["qualification_attempt"], directory)
            if observed:
                break
            if time.monotonic() > deadline:
                raise TimeoutError("Inspect existing qualification; do not replace it")
            time.sleep(1)
        state["qualifications"].append(observed)
        write(path, state)
        assert observed["termination"]["exitCode"] == 0 and observed["result"]["quality"] == 1
        assert observed["result"]["fixture_sha256"] == plan["fixture_sha256"]
        assert observed["result"]["accuracy"] >= plan["quality"]["minimum_accuracy"]
    failed = receipt(plan["experiment_id"] + "-f0-spark", directory)
    state["retained_failed_qualification"] = failed
    assert failed["termination"]["exitCode"] != 0
    profiles = {}
    for node, observed in zip(plan["pool"], state["qualifications"], strict=True):
        result = observed["result"]
        compute = result["elapsed_seconds"] / result["rounds"] * plan["main_rounds"]
        overhead = max(0, observed["gpu_seconds"] - result["elapsed_seconds"])
        profiles[node["ref"]] = dict(
            source_attempt=observed["attempt_id"],
            predicted_service_seconds=compute + overhead,
            per_forward_seconds=result["elapsed_seconds"] / result["rounds"],
            observed_reservation_seconds=observed["gpu_seconds"],
            scope="single native microprofile; work-size scaling is an estimate, not an observed main result",
        )
    upfront = state["qualifications"] + [failed]
    state["profiling"] = dict(
        source_attempts=[x["attempt_id"] for x in state["qualifications"]],
        gpu_seconds=sum(x["gpu_seconds"] for x in upfront),
        wall_seconds=max(x["finished_at"] for x in upfront)
        - min(epoch(x["job"]["metadata"]["creationTimestamp"]) for x in upfront),
        scope="all microprofile/qualification allocations including failed Spark attempt and runtime correction gap",
    )
    state["frozen_profiles"] = profiles
    orders = ["baseline", "reuse"]
    random.Random(plan["seed"]).shuffle(orders)
    state["orders"] = orders
    write(path, state)
    print(
        json.dumps(dict(phase="microprofiles_frozen", orders=orders, upfront=state["profiling"])),
        flush=True,
    )
    for arm in orders:
        cohort = dict(arm=arm, block=0, started_at=time.time(), jobs=[])
        state["cohorts"].append(cohort)
        backlog = {n["ref"]: 0 for n in plan["pool"]}
        for index in range(plan["jobs_per_arm"]):
            if arm == "baseline":
                node = plan["pool"][index % len(plan["pool"])]
            else:
                node = min(
                    plan["pool"],
                    key=lambda n: (
                        backlog[n["ref"]] + profiles[n["ref"]]["predicted_service_seconds"],
                        n["ref"],
                    ),
                )
                backlog[node["ref"]] += profiles[node["ref"]]["predicted_service_seconds"]
            base_name = (
                "f0-spark-runtime.json" if node["ref"] == "spark" else "f0-" + node["ref"] + ".json"
            )
            manifest = copy.deepcopy(json.loads((directory / base_name).read_text()))
            name = plan["experiment_id"] + "-" + arm + "-" + str(index)
            manifest["metadata"]["name"] = name
            manifest["spec"]["template"]["spec"]["containers"][0]["command"][-1] = str(
                plan["main_rounds"]
            )
            manifest_path = directory / (name + ".json")
            write(manifest_path, manifest)
            item = dict(
                attempt_id=name,
                node_ref=node["node"],
                candidate_ref=node["ref"],
                request_started_at=time.time(),
                requested_work=plan["main_rounds"],
                profile_source=profiles[node["ref"]]["source_attempt"] if arm == "reuse" else None,
            )
            cohort["jobs"].append(item)
            write(path, state)
            kube("create", "-f", str(manifest_path), structured=False)
        timeout = time.monotonic() + 300
        while True:
            for item in cohort["jobs"]:
                if "outcome" in item:
                    continue
                observed = receipt(item["attempt_id"], directory)
                if not observed:
                    continue
                item["native_receipt"] = observed
                write(path, state)
                result = observed["result"]
                assert observed["termination"]["exitCode"] == 0 and result["quality"] == 1, (
                    "Failed native attempt retained"
                )
                assert (
                    result["rounds"] == plan["main_rounds"]
                    and result["fixture_sha256"] == plan["fixture_sha256"]
                )
                item.update(
                    accepted_at=epoch(observed["job"]["metadata"]["creationTimestamp"]),
                    started_at=observed["scheduled_at"],
                    finished_at=observed["finished_at"],
                    gpu_seconds=observed["gpu_seconds"],
                    units=result["images"],
                    quality=result["quality"],
                    outcome="SUCCEEDED",
                    details=dict(
                        model_digest=plan["fixture_sha256"],
                        input_digest=plan["fixture_sha256"],
                        **result,
                    ),
                    sensor=[
                        dict(at=s["at"], utilization_percent=s["utilization"])
                        for s in result["sensor"]
                    ],
                )
                write(path, state)
                print(
                    json.dumps(
                        dict(
                            phase=arm,
                            candidate=item["candidate_ref"],
                            gpu_seconds=item["gpu_seconds"],
                            compute_seconds=result["elapsed_seconds"],
                        )
                    ),
                    flush=True,
                )
            if all("outcome" in item for item in cohort["jobs"]):
                break
            if time.monotonic() > timeout:
                raise TimeoutError("Incomplete cohort: preserve evidence and suspend owned Jobs")
            time.sleep(1)
        cohort["wall_seconds"] = max(j["finished_at"] for j in cohort["jobs"]) - min(
            j["accepted_at"] for j in cohort["jobs"]
        )
        cohort["started_at"] = min(j["accepted_at"] for j in cohort["jobs"])
        write(path, state)
    state["plan"]["jobs_per_cohort"] = plan["jobs_per_arm"]
    write(path, state)
    summary, _ = audit(directory)
    write(directory / "summary.json", summary)
    print(json.dumps(dict(phase="complete", summary=summary)), flush=True)


def retain_and_suspend_incomplete(directory):
    """Preserve partial evidence before suspending only this experiment's Jobs."""
    path = directory / "capture.json"
    if not path.exists():
        return
    state = json.loads(path.read_text())
    errors = []
    for cohort in state.get("cohorts", []):
        for item in cohort["jobs"]:
            if item.get("outcome"):
                continue
            try:
                native = kube("get", "job", item["attempt_id"], "-o", "json")
                if (
                    native["metadata"].get("labels", {}).get("resource-advisor/experiment")
                    != state["plan"]["experiment_id"]
                ):
                    raise ValueError("abort refused: unrelated native identity")
                pods = kube("get", "pods", "-l", "job-name=" + item["attempt_id"], "-o", "json")
                item["partial_native_evidence"] = dict(job=native, pods=pods)
                if pods["items"]:
                    try:
                        item["partial_native_evidence"]["log"] = kube(
                            "logs", "job/" + item["attempt_id"], "--tail=1000", structured=False
                        )
                    except subprocess.SubprocessError as exc:
                        item["partial_native_evidence"]["log_error"] = str(exc)
                write(path, state)
                if native.get("status", {}).get("succeeded") or native.get("status", {}).get(
                    "failed"
                ):
                    continue
                kube(
                    "patch",
                    "job",
                    item["attempt_id"],
                    "--type=merge",
                    "-p",
                    json.dumps(
                        {
                            "metadata": {"resourceVersion": native["metadata"]["resourceVersion"]},
                            "spec": {"suspend": True},
                        }
                    ),
                    structured=False,
                )
            except (subprocess.SubprocessError, ValueError, KeyError) as exc:
                errors.append(dict(attempt_id=item["attempt_id"], error=str(exc)))
    state["abort"] = dict(
        at=time.time(),
        errors=errors,
        status="owned incomplete native Jobs suspension requested; evidence retained",
    )
    write(path, state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    directory = parser.parse_args().directory.resolve()
    try:
        main(directory)
    except BaseException:
        retain_and_suspend_incomplete(directory)
        raise
