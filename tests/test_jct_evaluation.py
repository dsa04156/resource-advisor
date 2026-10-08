"""Behavioral checks at the saved-capture audit and promotion-gate seams."""

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from resource_advisor.jct_evaluation import audit_capture, evaluate_gate, summarize_capture

ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT / "docs/evidence/profile-pool-20261008-v2-results"
ARMS = ("round_robin", "profile_only", "profile_queue")
LOADS = {"sparse": 3.0, "moderate": 0.5, "burst": 0.0}


def timestamp(epoch):
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def native_job(plan, candidate, attempt, accepted, duration, profile=False):
    finish = accepted + duration
    detail = {
        "model_digest": plan["fixture_sha256"],
        "input_digest": plan["fixture_sha256"],
        "fixture_sha256": plan["fixture_sha256"],
        "kernel_sha256": plan["kernel_sha256"],
        "measured": True,
        "evidence_kind": "hardware",
        "outcome": "COMPLETED",
        "rounds": 16384,
        "batch_size": 256,
        "images": 4194304,
        "quality": 1,
        "accuracy": 0.98046875,
        "arch": candidate["arch"],
        "runtime_versions": {"cuda_driver_api": "13020"},
        "elapsed_seconds": 1,
        "compute_started_at": finish - 2,
        "compute_finished_at": finish - 1,
        "sensor": [],
    }
    receipt = {
        "attempt_id": attempt,
        "result": {k: v for k, v in detail.items() if k not in ("model_digest", "input_digest")},
        "termination": {
            "exitCode": 0,
            "startedAt": timestamp(accepted + 1),
            "finishedAt": timestamp(finish),
        },
        "scheduled_at": accepted + 1,
        "finished_at": finish,
        "gpu_seconds": duration - 1,
        "log_sha256": "0" * 64,
    }
    native_id = attempt
    if candidate["backend"] == "kubernetes":
        container = {
            "image": candidate["image"],
            "resources": {
                field: {"cpu": "1", "memory": "512Mi", candidate["resource_key"]: "1"}
                for field in ("requests", "limits")
            },
        }
        receipt["job"] = {
            "metadata": {
                "name": attempt,
                "uid": attempt + "-uid",
                "creationTimestamp": timestamp(accepted),
            },
            "spec": {"template": {"spec": {"containers": [container]}}},
        }
        receipt["pod"] = {
            "metadata": {
                "uid": attempt + "-pod",
                "ownerReferences": [{"kind": "Job", "name": attempt, "uid": attempt + "-uid"}],
            },
            "spec": {"nodeName": candidate["node"], "containers": [container]},
            "status": {
                "phase": "Succeeded",
                "conditions": [
                    {
                        "type": "PodScheduled",
                        "status": "True",
                        "lastTransitionTime": timestamp(accepted + 1),
                    }
                ],
                "containerStatuses": [
                    {"state": {"terminated": receipt["termination"]}, "restartCount": 0}
                ],
            },
        }
        if candidate.get("runtime_class"):
            receipt["job"]["spec"]["template"]["spec"]["runtimeClassName"] = candidate[
                "runtime_class"
            ]
            receipt["pod"]["spec"]["runtimeClassName"] = candidate["runtime_class"]
    else:
        native_id = str(int(hashlib.sha256(attempt.encode()).hexdigest()[:12], 16))
        receipt.update(
            native_id=native_id,
            native_state="COMPLETED",
            accepted_at=accepted,
            native_fields={
                "JobId": native_id,
                "JobName": attempt,
                "JobState": "COMPLETED",
                "Account": "ra-lab",
                "QOS": "ra-normal",
                "Partition": "compute",
                "ExitCode": "0:0",
                "NodeList": candidate["node"],
                "NumCPUs": "1",
                "MinMemoryNode": "512M",
                "ReqTRES": "cpu=1,mem=512M,node=1,gres/gpu=1",
                "AllocTRES": "cpu=1,mem=512M,node=1,gres/gpu=1",
                "TresPerNode": "gres/gpu:orin_nano:1",
                "SubmitTime": timestamp(accepted),
                "StartTime": timestamp(accepted + 1),
                "EndTime": timestamp(finish),
            },
        )
    return {
        "attempt_id": attempt,
        "candidate_ref": candidate["ref"],
        "backend": candidate["backend"],
        "native_id": native_id,
        "requested_work": 16384,
        "request_started_at": accepted - 0.1,
        "submit_response_at": accepted + 0.1,
        "accepted_at": accepted,
        "started_at": accepted + 1,
        "finished_at": finish,
        "gpu_seconds": duration - 1,
        "units": 4194304,
        "quality": 1,
        "outcome": "SUCCEEDED",
        "details": detail,
        "native_receipt": receipt,
        "sensor": [],
        "profile_source": None if profile else "profile-" + candidate["ref"],
    }


def capture_fixture():
    plan = json.loads((FROZEN / "plan.json").read_text())
    plan.update(
        paired_blocks=3,
        jobs_per_cohort=12,
        main_rounds=16384,
        qualification_rounds=16384,
        seed=20261008,
        loads=LOADS,
        orders=list(ARMS),
        kernel_sha256="241965dbd209e40184a62db4d13e54e7b3f1ec2cda43dda30fe31da17baef700",
    )
    for candidate in plan["pool"]:
        candidate["backend"] = "kubernetes"
        candidate["nominal_slots"] = 2 if ".shared" in candidate["resource_key"] else 1
    plan["pool"].append(
        {
            "ref": "slurm-orin",
            "node": "slurm-w2",
            "backend": "slurm",
            "arch": "arm64",
            "resource_key": "gres/gpu:orin_nano",
            "nominal_slots": 1,
        }
    )
    profiles = [
        native_job(plan, node, "profile-" + node["ref"], 1791450000 + i * 100, 10, profile=True)
        for i, node in enumerate(plan["pool"])
    ]
    cohorts = []
    for load, interval in LOADS.items():
        for block in range(3):
            for arm in ARMS:
                begin = 1791460000 + len(cohorts) * 100
                duration = (8 if load == "sparse" else 6) if arm == "profile_queue" else 10
                jobs = []
                for i in range(12):
                    node = plan["pool"][i % 6]
                    # Exact same intended arrival trace; fractional native clocks are allowed.
                    job = native_job(
                        plan, node, f"{load}-{block}-{arm}-{i}", begin + i * interval, duration
                    )
                    job["planned_arrival_offset"] = i * interval
                    if arm == "round_robin":
                        job["profile_source"] = None
                    jobs.append(job)
                cohorts.append(
                    {
                        "arm": arm,
                        "load": load,
                        "block": block,
                        "started_at": begin - 0.1,
                        "wall_seconds": 11 * interval + duration + 0.2,
                        "jobs": jobs,
                    }
                )
    return {
        "plan": plan,
        "qualifications": profiles,
        "cohorts": cohorts,
        "profiling": {
            "source_attempts": [j["attempt_id"] for j in profiles],
            "gpu_seconds": 54,
            "wall_seconds": 610,
        },
    }


def test_summary_keeps_cohort_replication_and_mixed_slot_units_explicit():
    summary = summarize_capture(capture_fixture())
    arm = summary["loads"]["burst"]["profile_queue"]
    assert arm["mean_jct_seconds"] == 6
    assert arm["p95_jct_seconds"] == 6
    assert arm["native_throughput_jobs_per_second"] == 2
    assert arm["cohort_repetitions"] == 3
    assert arm["jobs"] == 36
    assert arm["reservation_seconds"] == {
        "kubernetes_exclusive": 90,
        "kubernetes_shared_slot": 60,
        "slurm_gres": 30,
    }
    assert arm["physical_gpu_hours"] is None
    assert summary["profile_upfront"]["mixed_accelerator_slot_seconds"] == 54
    assert summary["statistical_significance_established"] is False


@pytest.mark.parametrize(
    "tamper",
    [
        "trace",
        "plan",
        "work",
        "fixture",
        "quality",
        "native_failure",
        "node",
        "cpu",
        "cost",
        "profile_leak",
        "native_identity",
        "clock",
    ],
)
def test_audit_rejects_incomplete_or_tampered_native_evidence(tamper):
    capture = capture_fixture()
    job = capture["cohorts"][0]["jobs"][0]
    if tamper == "trace":
        capture["cohorts"].pop()
    elif tamper == "plan":
        capture["plan"]["paired_blocks"] = 1
    elif tamper == "work":
        job["details"]["rounds"] = 8
    elif tamper == "fixture":
        job["details"]["fixture_sha256"] = "f" * 64
    elif tamper == "quality":
        job["details"]["quality"] = 0.8
    elif tamper == "native_failure":
        job["native_receipt"]["termination"]["exitCode"] = 1
    elif tamper == "node":
        job["native_receipt"]["pod"]["spec"]["nodeName"] = "foreign"
    elif tamper == "cpu":
        job["native_receipt"]["pod"]["spec"]["containers"][0]["resources"]["requests"]["cpu"] = "2"
    elif tamper == "cost":
        job["gpu_seconds"] = 0
    elif tamper == "profile_leak":
        job["attempt_id"] = capture["qualifications"][0]["attempt_id"]
    elif tamper == "native_identity":
        job["native_receipt"]["pod"]["metadata"]["ownerReferences"][0]["uid"] = "foreign"
    elif tamper == "clock":
        job["started_at"] = job["finished_at"] + 1
    with pytest.raises(ValueError):
        summarize_capture(capture)


def qualified_summary():
    summary = summarize_capture(capture_fixture())
    summary.update(evidence_complete=True, timing_ambiguities=[], native_jct_uncertainty_seconds=1)
    return summary


def test_gate_requires_every_pair_against_both_controls():
    summary = qualified_summary()
    assert evaluate_gate(summary)["passed"] is True
    # Pooled improvement still exceeds 10%, but one independent repeat loses.
    cohort = next(
        c
        for c in summary["cohorts"]
        if c["load"] == "burst" and c["arm"] == "profile_queue" and c["block"] == 2
    )
    cohort["mean_jct_seconds"] = 11
    result = evaluate_gate(summary)
    assert result["passed"] is False
    assert any("burst block 2" in reason for reason in result["reasons"])


@pytest.mark.parametrize(
    "change", ["incomplete", "ambiguity", "aggregate", "p95", "throughput", "sparse", "uncertainty"]
)
def test_gate_blocks_missing_evidence_or_each_preregistered_regression(change):
    summary = qualified_summary()
    if change == "incomplete":
        summary["evidence_complete"] = False
    elif change == "ambiguity":
        summary["timing_ambiguities"] = ["burst actual arrivals differ by arm"]
    elif change == "aggregate":
        for load in ("moderate", "burst"):
            summary["loads"][load]["profile_queue"]["mean_jct_seconds"] = 9.5
    elif change == "p95":
        summary["loads"]["burst"]["profile_queue"]["p95_jct_seconds"] = 11
    elif change == "throughput":
        summary["loads"]["burst"]["profile_queue"]["native_throughput_jobs_per_second"] = 1
    elif change == "sparse":
        summary["loads"]["sparse"]["profile_queue"]["mean_jct_seconds"] = 11
    else:
        summary["native_jct_uncertainty_seconds"] = 5
    assert evaluate_gate(summary)["passed"] is False


def write_archive(directory, capture=None):
    capture = capture if capture is not None else capture_fixture()
    (directory / "jobs").mkdir()
    for job in [
        *capture["qualifications"],
        *(j for cohort in capture["cohorts"] for j in cohort["jobs"]),
    ]:
        raw = "POOL_INFERENCE_RESULT " + json.dumps(job["native_receipt"]["result"]) + "\n"
        (directory / (job["attempt_id"] + ".log")).write_text(raw)
        job["native_receipt"]["log_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
    for cohort in capture["cohorts"]:
        refs = []
        for job in cohort["jobs"]:
            filename = "jobs/" + job["attempt_id"] + ".json"
            raw = json.dumps(job).encode()
            (directory / filename).write_bytes(raw)
            refs.append({"job_file": filename, "job_sha256": hashlib.sha256(raw).hexdigest()})
        cohort["jobs"] = refs
    (directory / "source.json").write_bytes((FROZEN / "source.json").read_bytes())
    (directory / "plan.json").write_text(json.dumps(capture["plan"]))
    (directory / "capture.json").write_text(json.dumps(capture))
    return capture


def test_archive_replays_job_files_and_exports_input_hashes(tmp_path):
    write_archive(tmp_path)
    report = audit_capture(tmp_path)
    assert report["summary"]["main_jobs"] == 324
    assert len(report["main_rows"]) == 324
    assert len(report["cohort_rows"]) == 27
    assert "jobs/sparse-0-round_robin-0.json" in report["input_hashes"]
    assert report["summary"]["archive_integrity_verified"] is True
    assert report["summary"]["gate"]["passed"] is False


@pytest.mark.parametrize(
    "tamper", ["file_hash", "escape", "source", "plan", "log", "duplicate_result"]
)
def test_archive_rejects_hash_substitution_and_path_escape(tmp_path, tamper):
    capture = write_archive(tmp_path)
    ref = capture["cohorts"][0]["jobs"][0]
    if tamper == "file_hash":
        (tmp_path / ref["job_file"]).write_text("{}")
    elif tamper == "escape":
        ref["job_file"] = "../foreign.json"
    elif tamper == "source":
        source = json.loads((tmp_path / "source.json").read_text())
        source["data"]["workload.py"] += "\n# tampered\n"
        (tmp_path / "source.json").write_text(json.dumps(source))
    elif tamper == "plan":
        capture["plan"]["seed"] = 1
    else:
        log = tmp_path / "sparse-0-round_robin-0.log"
        raw = log.read_bytes()
        log.write_bytes(raw + (b"extra" if tamper == "log" else raw))
        if tamper == "duplicate_result":
            job_path = tmp_path / ref["job_file"]
            job = json.loads(job_path.read_text())
            job["native_receipt"]["log_sha256"] = hashlib.sha256(log.read_bytes()).hexdigest()
            raw = json.dumps(job).encode()
            job_path.write_bytes(raw)
            ref["job_sha256"] = hashlib.sha256(raw).hexdigest()
    (tmp_path / "capture.json").write_text(json.dumps(capture))
    with pytest.raises(ValueError):
        audit_capture(tmp_path)


def archive_one_choice(directory, capture, future=False):
    from dataclasses import asdict

    from resource_advisor.jct_selection import CandidateIdentity, select_candidate

    ref = capture["cohorts"][0]["jobs"][0]
    job_path = directory / ref["job_file"]
    job = json.loads(job_path.read_text())
    selected_at = job["request_started_at"] + (10 if future else -0.1)
    identities = [
        CandidateIdentity(
            n["ref"],
            n["backend"],
            "local-native-pool",
            n["node"],
            n["resource_key"],
            n.get("image", "slurm-python3-cuda-driver") + ":" + n.get("runtime_class", "default"),
            capture["plan"]["source_sha256"] + ":" + capture["plan"]["fixture_sha256"],
        )
        for n in capture["plan"]["pool"]
    ]
    snapshot = {"observed_at": selected_at - 1, "finished_at": selected_at - 0.9}
    (directory / "snapshot.json").write_text(json.dumps(snapshot))
    choice = {
        "policy": "round_robin",
        "candidate_ref": job["candidate_ref"],
        "decision_started_at": selected_at - 0.01,
        "selection_at": selected_at,
        "decision_finished_at": selected_at + 0.01,
        "rr_index": 0,
        "decision": asdict(
            select_candidate("round_robin", identities, [], now_seconds=selected_at)
        ),
        "fallback": None,
        "profiles": [],
        "profile_source_refs": [],
        "queues": [],
        "intents": [],
        "cohort_backlog_seconds": None,
        "snapshot_ref": "snapshot.json",
        "snapshot_sha256": hashlib.sha256((directory / "snapshot.json").read_bytes()).hexdigest(),
    }
    (directory / "choice.json").write_text(json.dumps(choice))
    job["choice_evidence"] = {
        "choice_file": "choice.json",
        "choice_sha256": hashlib.sha256((directory / "choice.json").read_bytes()).hexdigest(),
    }
    raw = json.dumps(job).encode()
    job_path.write_bytes(raw)
    ref["job_sha256"] = hashlib.sha256(raw).hexdigest()
    (directory / "capture.json").write_text(json.dumps(capture))


def test_archive_rejects_a_choice_recorded_after_its_outcome(tmp_path):
    capture = write_archive(tmp_path)
    archive_one_choice(tmp_path, capture, future=True)
    with pytest.raises(ValueError, match="pre-outcome"):
        audit_capture(tmp_path)


def test_archive_replays_the_committed_policy_and_rejects_changed_decision(tmp_path):
    capture = write_archive(tmp_path)
    archive_one_choice(tmp_path, capture)
    assert audit_capture(tmp_path)["summary"]["decisions_replayed"] == 1
    choice_path = tmp_path / "choice.json"
    choice = json.loads(choice_path.read_text())
    choice["decision"]["candidate_ref"] = "rtx5080"
    choice_path.write_text(json.dumps(choice))
    ref = capture["cohorts"][0]["jobs"][0]
    job_path = tmp_path / ref["job_file"]
    job = json.loads(job_path.read_text())
    job["choice_evidence"]["choice_sha256"] = hashlib.sha256(choice_path.read_bytes()).hexdigest()
    job_path.write_text(json.dumps(job))
    ref["job_sha256"] = hashlib.sha256(job_path.read_bytes()).hexdigest()
    (tmp_path / "capture.json").write_text(json.dumps(capture))
    with pytest.raises(ValueError, match="decision replay"):
        audit_capture(tmp_path)


def write_clock_proof(directory, capture):
    refs = [n["ref"] for n in capture["plan"]["pool"]] + ["slurm-controller"]
    for phase, at in (("before", 1791459000.0), ("after", 1791470000.0)):
        observations = []
        for ref in refs:
            samples = []
            for i in range(3):
                before, after, remote = at + i, at + i + 0.2, at + i + 0.1
                samples.append(
                    {
                        "local_before": before,
                        "local_after": after,
                        "remote_epoch": remote,
                        "rtt_seconds": after - before,
                        "offset_bounds": [remote - after, remote - before],
                        "qualified": True,
                    }
                )
            observations.append(
                {
                    "candidate": ref,
                    "samples": samples,
                    "best": samples[0],
                    "qualified": True,
                    "transport": "read-only epoch",
                }
            )
        (directory / f"clock-alignment-{phase}-qualified.json").write_text(
            json.dumps(
                {
                    "qualified": True,
                    "maximum_clock_offset_bound_seconds": 1,
                    "observations": observations,
                }
            )
        )


def test_archive_qualifies_clock_bounds_only_from_pre_and_post_raw_probes(tmp_path):
    capture = write_archive(tmp_path)
    write_clock_proof(tmp_path, capture)
    summary = audit_capture(tmp_path)["summary"]
    assert summary["clock_alignment_qualified"] is True
    assert summary["native_jct_uncertainty_seconds"] == pytest.approx(1.2)
    (tmp_path / "clock-alignment-after-qualified.json").unlink()
    assert audit_capture(tmp_path)["summary"]["clock_alignment_qualified"] is False


def test_archive_rejects_fabricated_clock_qualification(tmp_path):
    capture = write_archive(tmp_path)
    write_clock_proof(tmp_path, capture)
    path = tmp_path / "clock-alignment-before-qualified.json"
    proof = json.loads(path.read_text())
    proof["observations"][0]["samples"][0]["offset_bounds"] = [0, 0]
    path.write_text(json.dumps(proof))
    with pytest.raises(ValueError, match="clock"):
        audit_capture(tmp_path)


def use_sacct_receipt(capture):
    job = capture["cohorts"][0]["jobs"][5]
    receipt = job["native_receipt"]
    fields = receipt.pop("native_fields")
    receipt["native_accounting"] = {
        "native_id": fields["JobId"],
        "name": fields["JobName"],
        "state": "COMPLETED",
        "exit_code": "0:0",
        "account": "ra-lab",
        "qos": "ra-normal",
        "nodes": fields["NodeList"],
        "req_tres": fields["ReqTRES"],
        "alloc_tres": fields["AllocTRES"],
        "submit": fields["SubmitTime"],
        "start": fields["StartTime"],
        "end": fields["EndTime"],
        "elapsed_raw": str(int(job["gpu_seconds"])),
        "accepted_at": job["accepted_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
    }
    return job


def test_audit_accepts_the_same_allocation_in_scontrol_or_primary_sacct_schema():
    capture = capture_fixture()
    use_sacct_receipt(capture)
    assert summarize_capture(capture)["main_jobs"] == 324


@pytest.mark.parametrize("tamper", ["gpu", "memory", "end", "step", "state"])
def test_audit_rejects_wrong_primary_sacct_allocation(tamper):
    capture = capture_fixture()
    row = use_sacct_receipt(capture)["native_receipt"]["native_accounting"]
    if tamper == "gpu":
        row["alloc_tres"] = row["alloc_tres"].replace("gres/gpu=1", "gres/gpu=2")
    elif tamper == "memory":
        row["req_tres"] = row["req_tres"].replace("512M", "1024M")
    elif tamper == "end":
        row["end"] = timestamp(row["finished_at"] + 10)
    elif tamper == "step":
        row["native_id"] += ".batch"
    else:
        row["state"] = "FAILED"
    with pytest.raises(ValueError):
        summarize_capture(capture)


def test_summary_preserves_source_specific_sensor_windows_and_net_profile_charge():
    capture = capture_fixture()
    job = capture["cohorts"][0]["jobs"][0]
    points = [
        {"at": 0, "source": "nvml-physical-uuid", "utilization": 20, "error": None},
        {"at": 0.5, "source": "nvml-physical-uuid", "utilization": 40, "error": None},
        {"at": 1, "source": "sysfs-shared", "utilization": 100, "error": None},
    ]
    job["details"]["sensor"] = points
    job["native_receipt"]["result"]["sensor"] = points
    summary = summarize_capture(capture)
    sensor = summary["sensors_by_native_job"][0]
    assert sensor["sampled_load_percent"] == 30
    assert sensor["observed_seconds"] == 0.5
    assert sensor["per_job_attribution_established"] is False
    cost = summary["net_cost"]["profile_queue_vs_round_robin"]
    assert cost["profile_upfront_charged_seconds"] == 54
    assert cost["queue_with_profile_mixed_slot_seconds"] == 666
    assert cost["physical_gpu_cost_reduction_established"] is False


def test_cli_reproduces_json_csv_and_output_hashes(tmp_path):
    directory = tmp_path / "archive"
    directory.mkdir()
    write_archive(directory)
    output = tmp_path / "report"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "examples/analyze_jct_comparison.py"),
            "--directory",
            str(directory),
            "--output",
            str(output),
        ],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads((output / "summary.json").read_text())
    assert summary["main_jobs"] == 324
    assert len((output / "main.csv").read_text().splitlines()) == 325
    manifest = json.loads((output / "manifest.json").read_text())
    assert (
        manifest["outputs"]["summary.json"]
        == hashlib.sha256((output / "summary.json").read_bytes()).hexdigest()
    )


def test_archive_retains_actual_arrival_jitter_and_rejects_changed_trace(tmp_path):
    capture = write_archive(tmp_path)
    ref = capture["cohorts"][0]["jobs"][1]
    path = tmp_path / ref["job_file"]
    job = json.loads(path.read_text())
    job["request_started_at"] += 0.25
    job["submit_response_at"] += 0.25
    path.write_text(json.dumps(job))
    ref["job_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    (tmp_path / "capture.json").write_text(json.dumps(capture))
    summary = audit_capture(tmp_path)["summary"]
    assert summary["arrival_evidence"][0]["request_trace_deviation_seconds"] == 0.25
    assert summary["arrival_sensitivity_bound_seconds"] == 0.25
    job["planned_arrival_offset"] = 0
    path.write_text(json.dumps(job))
    ref["job_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    (tmp_path / "capture.json").write_text(json.dumps(capture))
    with pytest.raises(ValueError, match="arrival"):
        audit_capture(tmp_path)


def test_archive_rejects_duplicate_raw_sacct_primary_even_with_rehashed_evidence(tmp_path):
    capture = capture_fixture()
    use_sacct_receipt(capture)
    capture = write_archive(tmp_path, capture)
    ref = capture["cohorts"][0]["jobs"][5]
    job_path = tmp_path / ref["job_file"]
    job = json.loads(job_path.read_text())
    row = job["native_receipt"]["native_accounting"]
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
    line = "|".join(row[k] for k in keys) + "\n"
    raw_snapshot = {"raw": {"slurm": {"accounting": {"exit": 0, "stdout": line, "stderr": ""}}}}
    snapshot_path = tmp_path / "terminal-snapshot.json"
    for raw in (line, line + line):
        raw_snapshot["raw"]["slurm"]["accounting"]["stdout"] = raw
        snapshot_path.write_text(json.dumps(raw_snapshot))
        job["native_receipt"].update(
            native_snapshot_ref="terminal-snapshot.json",
            native_snapshot_sha256=hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
        )
        job_path.write_text(json.dumps(job))
        ref["job_sha256"] = hashlib.sha256(job_path.read_bytes()).hexdigest()
        (tmp_path / "capture.json").write_text(json.dumps(capture))
        if raw == line:
            assert audit_capture(tmp_path)["summary"]["main_jobs"] == 324
        else:
            with pytest.raises(ValueError, match="primary"):
                audit_capture(tmp_path)


def test_user_shortening_preserves_all_27_cohorts_and_three_independent_repeats():
    capture = capture_fixture()
    capture["plan"]["jobs_per_cohort"] = 6
    for cohort in capture["cohorts"]:
        cohort["jobs"] = cohort["jobs"][:6]
    summary = summarize_capture(capture)
    assert summary["main_jobs"] == summary["expected_main_jobs"] == 162
    assert len(summary["cohorts"]) == 27
    assert summary["loads"]["burst"]["profile_queue"]["cohort_repetitions"] == 3
    capture["cohorts"][0]["jobs"].pop()
    with pytest.raises(ValueError, match="incomplete"):
        summarize_capture(capture)


@pytest.mark.parametrize(
    "tamper",
    ["round_sum", "round_count", "round_nan", "future_compute", "changed_pool", "naive_slurm"],
)
def test_audit_rejects_fabricated_compute_or_changed_preregistered_route(tamper):
    capture = capture_fixture()
    job = capture["cohorts"][0]["jobs"][0]
    detail = job["details"]
    if tamper == "round_sum":
        detail["round_seconds"] = [0] * 16384
    elif tamper == "round_count":
        detail["round_seconds"] = [1]
    elif tamper == "round_nan":
        detail["round_seconds"] = [float("nan")] * 16384
    elif tamper == "future_compute":
        detail["compute_finished_at"] = job["finished_at"] + 10
    elif tamper == "changed_pool":
        capture["plan"]["pool"][0]["node"] = "replacement-node"
        for cohort in capture["cohorts"]:
            for j in cohort["jobs"]:
                if j["candidate_ref"] == "rtx5060":
                    j["native_receipt"]["pod"]["spec"]["nodeName"] = "replacement-node"
        capture["qualifications"][0]["native_receipt"]["pod"]["spec"]["nodeName"] = (
            "replacement-node"
        )
    else:
        fields = capture["cohorts"][0]["jobs"][5]["native_receipt"]["native_fields"]
        for key in ("SubmitTime", "StartTime", "EndTime"):
            fields[key] = fields[key].replace("Z", "")
    job["native_receipt"]["result"] = {
        k: v for k, v in detail.items() if k not in ("model_digest", "input_digest")
    }
    with pytest.raises(ValueError):
        summarize_capture(capture)


def test_archive_rejects_complete_queue_claim_that_omits_own_workload(tmp_path):
    capture = write_archive(tmp_path)
    archive_one_choice(tmp_path, capture)
    choice_path = tmp_path / "choice.json"
    snapshot_path = tmp_path / "snapshot.json"
    choice = json.loads(choice_path.read_text())
    snapshot = json.loads(snapshot_path.read_text())
    node = capture["plan"]["pool"][0]
    native_job = {
        "kind": "Job",
        "metadata": {"name": "queued-own", "uid": "own-uid"},
        "spec": {"template": {"spec": {"nodeSelector": {"kubernetes.io/hostname": node["node"]}}}},
        "status": {"active": 1},
    }
    native_lists = {"jobs_pods": {"items": [native_job]}, "workloads": {"items": []}}
    snapshot.update(
        raw={"kubernetes": {"items": [native_job], "native_lists": native_lists}},
        jobs=[native_job],
        pods=[],
        workloads=[],
    )
    snapshot_path.write_text(json.dumps(snapshot))
    first_prediction = choice["decision"]["predictions"][0]
    identity = {
        "candidate_ref": first_prediction["candidate_ref"],
        "backend": "kubernetes",
        "cluster_ref": "local-native-pool",
        "node_ref": node["node"],
        "resource_key": node["resource_key"],
        "runtime_digest": node["image"] + ":default",
        "workload_digest": capture["plan"]["source_sha256"]
        + ":"
        + capture["plan"]["fixture_sha256"],
    }
    choice["queues"] = [
        {
            "identity": identity,
            "snapshot_ref": "snapshot.json",
            "observed_at": snapshot["observed_at"],
            "capacity": 1,
            "complete": True,
            "running": [],
            "pending": [],
            "capacity_unit": "physical_device",
            "observed_job_ids": ["queued-own"],
        }
    ]
    choice["snapshot_sha256"] = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    choice_path.write_text(json.dumps(choice))
    ref = capture["cohorts"][0]["jobs"][0]
    job_path = tmp_path / ref["job_file"]
    job = json.loads(job_path.read_text())
    job["choice_evidence"]["choice_sha256"] = hashlib.sha256(choice_path.read_bytes()).hexdigest()
    job_path.write_text(json.dumps(job))
    ref["job_sha256"] = hashlib.sha256(job_path.read_bytes()).hexdigest()
    (tmp_path / "capture.json").write_text(json.dumps(capture))
    with pytest.raises(ValueError, match="Workload"):
        audit_capture(tmp_path)


def test_audit_accepts_successful_container_exit_before_pod_phase_reconciliation():
    capture = capture_fixture()
    pod = capture["cohorts"][0]["jobs"][0]["native_receipt"]["pod"]
    pod["status"]["phase"] = "Running"
    assert summarize_capture(capture)["main_jobs"] == 324
    pod["status"]["phase"] = "Failed"
    with pytest.raises(ValueError, match="Pod"):
        summarize_capture(capture)
