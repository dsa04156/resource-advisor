"""Recompute total research usage, preserving censored attempts and typed units."""

import argparse
import hashlib
import json
import math
from pathlib import Path

from audit_right_sizing_trial import audit_hailo_feedback, compare_reference, require

from resource_advisor.uncertainty import stamp


def audit_cost(capture, evidence_root):
    inputs = {}
    for name, digest in capture["input_sha256"].items():
        require(Path(name).name == name, "input path must be an evidence basename")
        raw = (evidence_root / name).read_bytes()
        require(hashlib.sha256(raw).hexdigest() == digest, "cost input digest mismatch")
        inputs[name] = json.loads(raw)
    report = inputs["right-sizing-gpu-v1.json"]
    plan = inputs["right-sizing-trial-plan-v1.json"]
    reference = inputs["right-sizing-reference-recovery-v2.json"]
    reference_plan = inputs["right-sizing-reference-recovery-plan-v2.json"]
    hailo = inputs["right-sizing-hailo-v1.json"]
    comparison = compare_reference(report, plan, reference, reference_plan)
    npu = audit_hailo_feedback(hailo, inputs["hailo-resnet50-inputs.json"])
    hailo_cost = inputs["right-sizing-hailo-cost-v1.json"]
    require(
        hailo_cost["raw_sha256"] == capture["input_sha256"]["right-sizing-hailo-v1.json"],
        "Hailo cost scope mismatch",
    )
    require(
        capture["primary_experiment_id"] == report["experiment_id"]
        and capture["reference_experiment_id"] == reference["experiment_id"],
        "cost experiment mismatch",
    )
    protocols = {}
    for name, raw, frozen in (
        ("primary", report, plan),
        ("supplementary", reference, reference_plan),
    ):
        times = capture["protocols"][name]
        start, end = stamp(times["started_at"]), stamp(times["ended_at"])
        require(
            end == max(stamp(s["terminal_observed_at"]) for s in raw["studies"]),
            "protocol end mismatch",
        )
        require(
            start <= min(stamp(s["request_started_at"]) for s in raw["studies"]),
            "protocol start after execution",
        )
        seconds = (end - start).total_seconds()
        require(
            0 < seconds <= frozen["whole_protocol_wall_seconds"], "whole protocol budget violation"
        )
        protocols[name] = {"elapsed_seconds": seconds, **times}
    all_gpu = report["attempts"] + report["qualifications"] + reference["attempts"]
    gpu_uids = {a["native"]["job_uid"] for a in all_gpu}
    npu_uids = {r["backend"]["uid"] for r in hailo["runs"]} | {hailo["qualification"]["job_uid"]}
    require(not gpu_uids & npu_uids, "reused cross-device Job")
    npu_cpu = sum(r["ledger"]["body"]["allocated_cpu_seconds"] for r in hailo["runs"])
    q = hailo_cost["qualification"]
    require(
        q["job_uid"] == hailo["qualification"]["job_uid"]
        and q["npu_reservation_seconds"]
        == hailo["qualification"]["scheduled_to_container_finished_seconds"],
        "Hailo qualification cost mismatch",
    )
    require(
        q["cpu_core_seconds"]
        == q["npu_reservation_seconds"] * hailo["binding"]["context"]["resources"]["host_cpu"],
        "Hailo qualification CPU mismatch",
    )
    npu_cpu += q["cpu_core_seconds"]
    require(math.isclose(npu_cpu, hailo_cost["total_cpu_core_seconds"]), "Hailo CPU total mismatch")
    require(
        npu["total_npu_reservation_seconds"] == hailo_cost["total_npu_reservation_seconds"],
        "Hailo reservation mismatch",
    )
    starts = [stamp(p["started_at"]) for p in protocols.values()]
    ends = [stamp(p["ended_at"]) for p in protocols.values()]
    return {
        "schema_version": "right-sizing-total-cost-audit-v1",
        "input_sha256": capture["input_sha256"],
        "experiments": [report["experiment_id"], reference["experiment_id"]],
        "native_gpu_jobs": len(all_gpu),
        "native_npu_jobs": len(npu_uids),
        "native_total_jobs": len(all_gpu) + len(npu_uids),
        "gpu_reservation_seconds": sum(a["device_seconds"] for a in all_gpu),
        "npu_reservation_seconds": npu["total_npu_reservation_seconds"],
        "cpu_reservation_core_seconds": sum(a["cpu_core_seconds"] for a in all_gpu) + npu_cpu,
        "gpu_attempts_host_memory_reservation_mib_seconds": sum(
            a["memory_mib_seconds"] for a in all_gpu
        ),
        "failed_gpu_jobs": comparison["failures_including_original_reference"],
        "protocols": protocols,
        "observed_research_wall_envelope_seconds": (max(ends) - min(starts)).total_seconds(),
        "hailo_protocol_wall_seconds": hailo_cost["observed_protocol_wall_seconds"],
        "timing_scope": "GPU controller start to final reference terminal observation, including intervening control waits. Hailo ran concurrently: do not add its wall time. Artifact readback/deployment/testing after execution are outside this envelope.",
        "cost_scope": "All fresh GPU/NPU qualifications, probes, independent confirmations, main runs and three failed references. GPU/NPU reservations remain separate. Finite-N deployment curves charge qualification/setup once per arm; characterization is additional research usage.",
        "unknown": capture["unknown"],
        "overall_goal_complete": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit_cost(json.loads(args.capture.read_text()), args.capture.parent)
    rendered = json.dumps(result, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered)
