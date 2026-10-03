"""Offline consistency audit of the bounded Hailo platform evidence, not hardware replay."""

import argparse
import json
import math
import statistics
from datetime import datetime
from pathlib import Path

from resource_advisor.contracts import ExecutionResult, WorkloadSpec, signature
from resource_advisor.hailo_benchmark import Binding, validate_binding
from resource_advisor.hailo_qualification import quality_gates


def require(condition, message):
    if not condition:
        raise ValueError(message)


def audit(data, manifest):
    binding = Binding.model_validate(data["binding"])
    workload = WorkloadSpec.model_validate(data["workload"])
    require(signature(binding) == data["binding_digest"], "binding digest mismatch")
    require(workload.identity == binding.identity, "workload identity mismatch")
    require(workload.candidates[0].context == binding.context, "workload context mismatch")
    runs = data["runs"]
    require(len(runs) == 4, "exactly three observations and one verification required")
    require([r["index"] for r in runs] == list(range(4)), "run ordering mismatch")
    require([r["mode"] for r in runs] == ["observe"] * 3 + ["fixed"], "run modes mismatch")
    for field in ["attempt_id", "job_id"]:
        require(len({r["result"][field] for r in runs}) == 4, "duplicate attempt or job")
    require(len({r["backend"]["uid"] for r in runs}) == 4, "duplicate scheduler Job")
    require(len({r["delivery"]["mlflow_run_id"] for r in runs}) == 4, "duplicate MLflow run")
    require(len({r["result"]["context_signature"] for r in runs}) == 1, "context changed")
    require(len(manifest["samples"]) == 100, "fixed accuracy cohort required")
    expected_inputs = [r["tensor_sha256"] for r in manifest["samples"]]
    require(len(set(expected_inputs)) == 100, "independent image tensors required")
    for row in runs:
        result = ExecutionResult.model_validate(row["result"])
        require(signature(result) == row["result_digest"], "result digest mismatch")
        require(result.outcome == "COMPLETED", "failed run cannot support completion")
        validate_binding(
            binding,
            {
                "RA_JOB_ID": result.job_id,
                "RA_ATTEMPT_ID": result.attempt_id,
                "RA_EPOCH": str(result.epoch),
                "RA_WORKLOAD_SIGNATURE": result.workload_signature,
                "RA_CONTEXT_SIGNATURE": result.context_signature,
                "RA_CONTEXT_JSON": binding.context.model_dump_json(),
                "RA_WORK_UNITS": str(workload.identity.work_units),
                "RA_INPUT_SHAPE": json.dumps(workload.identity.input_shape),
                "RA_PRECISION": workload.identity.precision,
                "RA_SEED": str(workload.identity.seed),
                "RA_EXECUTION_MODE": row["mode"],
            },
        )
        report, measurements = row["report"], result.measurements
        require(signature(report) == row["report_digest"], "report digest mismatch")
        require(report["manifest_sha256"] == binding.manifest_digest[7:], "fixture changed")
        require(report["hef_sha256"] == binding.hef_digest[7:], "compiled model changed")
        require(
            report["original_model_sha256"] == binding.identity.model_digest[7:], "model changed"
        )
        for name, version in binding.context.runtime_versions.items():
            require(report[name] == version, "runtime changed")
        predictions = report["predictions"]
        require([p["input_sha256"] for p in predictions] == expected_inputs, "input order changed")
        for p, expected in zip(predictions, manifest["samples"], strict=True):
            require(p["label"] == expected["label"], "ground truth changed")
            require(p["reference_top1"] == expected["reference_top1"], "reference changed")
        quality = quality_gates(
            [p["prediction"] for p in predictions],
            [p["label"] for p in predictions],
            [p["reference_top1"] for p in predictions],
        )
        require(quality == report["quality"] and quality["qualified"], "quality gate failed")
        require(measurements.quality_value == quality["accuracy"], "accuracy mismatch")
        require(measurements.work_units == measurements.sample_count == 100, "work units changed")
        elapsed = math.fsum(p["elapsed_seconds"] for p in predictions)
        require(
            math.isclose(elapsed, measurements.elapsed_seconds, rel_tol=1e-12, abs_tol=1e-12),
            "call duration mismatch",
        )
        require(
            measurements.elapsed_seconds == report["elapsed_seconds"], "timing boundary mismatch"
        )
        require(
            measurements.peak_memory_mib == report["host_process_peak_rss_mib"],
            "host memory scope mismatch",
        )
        require(
            measurements.peak_memory_mib <= workload.quality.maximum_peak_memory_mib,
            "memory gate failed",
        )
        require(
            measurements.gpu_utilization is None and measurements.power_watts is None,
            "invented sensor data",
        )
        backend, ledger = row["backend"], row["ledger"]
        require(backend["image_digest"] == data["platform_image_digest"], "image changed")
        require(backend["exit_code"] == 0 and backend["single_pod"], "scheduler outcome mismatch")
        resources = {"cpu": "1", "memory": "1Gi", "hailo.ai/h8": "1"}
        require(
            backend["resources"] == {"requests": resources, "limits": resources},
            "allocation changed",
        )
        seconds = (
            datetime.fromisoformat(backend["container_finished_at"])
            - datetime.fromisoformat(backend["scheduled_at"])
        ).total_seconds()
        require(ledger["allocated_device_seconds"] == seconds, "allocation duration mismatch")
        require(ledger["attempt_id"] == result.attempt_id, "ledger owner mismatch")
        require(ledger["device_class"] == "npu", "NPU accounting unit mismatch")
        require(
            ledger["measured_compute_seconds"] == measurements.elapsed_seconds,
            "compute ledger mismatch",
        )
        require(row["profile_ref"] == result.attempt_id, "profile owner mismatch")
        require(
            row["delivery"]["ledger_rows"] == row["delivery"]["mlflow_runs"] == 1,
            "duplicate delivery",
        )
        require(
            row["delivery"]["matching_s3_api_mlflow_bytes"], "live delivery verification absent"
        )
    rec, approval = data["recommendation"], data["approval"]
    require(
        rec["digest"] == signature({k: v for k, v in rec.items() if k != "digest"}),
        "recommendation digest mismatch",
    )
    require(rec["workload_digest"] == signature(workload), "recommendation scope mismatch")
    require(
        approval["recommendation_ref"] == rec["ref"]
        and approval["recommendation_digest"] == rec["digest"],
        "approval mismatch",
    )
    require(runs[3]["approval_ref"] == approval["ref"], "verification approval missing")
    rank = rec["ranking"][0]
    require(
        rank["evidence_refs"] == [r["result"]["attempt_id"] for r in runs[:3]],
        "future verification leaked into lookup",
    )
    require(rank["independent_runs"] == 3, "incorrect timing replication count")
    require(
        datetime.fromisoformat(rec["created_at"])
        < datetime.fromisoformat(runs[3]["backend"]["created_at"])
        < datetime.fromisoformat(approval["expires_at"]),
        "approval chronology invalid",
    )
    observed = [r["result"]["measurements"]["elapsed_seconds"] for r in runs[:3]]
    require(
        math.isclose(statistics.mean(observed), rank["mean_seconds"], rel_tol=1e-12),
        "lookup mean mismatch",
    )
    verification = runs[3]["result"]["measurements"]["elapsed_seconds"]
    return {
        "verified_api_jobs": 4,
        "qualification_jobs": 1,
        "independent_accuracy_images": 100,
        "api_npu_reservation_seconds": math.fsum(
            r["ledger"]["allocated_device_seconds"] for r in runs
        ),
        "qualification_npu_reservation_seconds": data["qualification"][
            "scheduled_to_container_finished_seconds"
        ],
        "observation_mean_call_seconds": statistics.mean(observed),
        "verification_call_seconds": verification,
        "later_verification_inside_mean_interval": rank["interval_seconds"][0]
        <= verification
        <= rank["interval_seconds"][1],
        "scope": "Offline consistency checks; live storage read-backs remain operator-attested evidence.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            audit(json.loads(args.evidence.read_text()), json.loads(args.manifest.read_text())),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
