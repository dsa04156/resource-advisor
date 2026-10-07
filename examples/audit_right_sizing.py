"""Compose existing raw-evidence auditors; never submit work or synthesize results."""

import argparse
import hashlib
import json
from pathlib import Path

from audit_contract_boundaries import audit as audit_contract
from audit_hailo_platform import audit as audit_hailo
from audit_load_drift import audit as audit_drift
from evaluate_operational_comparison import summarize as audit_operational
from evaluate_policy_comparison import summarize as audit_policy
from evaluate_transfer import summarize as audit_transfer

from resource_advisor.contracts import ExecutionResult, signature
from resource_advisor.uncertainty import stamp

INPUTS = {
    "contract": "contract-boundaries-v1.json",
    "policy": "policy-comparison-v2.json",
    "policy_plan": "policy-comparison-plan-v2.json",
    "predecessor": "policy-stop.json",
    "operational": "operational-comparison-v1.json",
    "operational_plan": "operational-plan-v1.json",
    "transfer": "transfer-gpu.json",
    "drift": "load-drift-v1.json",
    "hailo": "hailo-resnet50-platform.json",
    "hailo_inputs": "hailo-resnet50-inputs.json",
}


def audit_policy_envelopes(report):
    """Add strict captured hardware envelope checks to the existing policy auditor."""
    studies = {s["ref"]: s for s in report["studies"].values()}
    native_ids = set()
    for row in report["observations"]:
        result = ExecutionResult.model_validate(row["result"])
        study = studies[row["study_ref"]]
        if (
            signature(row["result"]) != row["result_digest"]
            or result.job_id != row["job_id"]
            or result.attempt_id != row["attempt_id"]
            or result.workload_signature != signature(study["spec"]["identity"])
            or result.evidence_kind != "hardware"
            or result.outcome != row["outcome"]
            or result.measurements.model_dump(mode="json") != row["measurements"]
        ):
            raise ValueError("hardware result digest/identity/measurement mismatch")
        native_id = row["backend_job_id"]
        if not native_id or native_id in native_ids:
            raise ValueError("missing/duplicate native attempt")
        native_ids.add(native_id)
        times = [
            stamp(row.get(k)) for k in ("submitted_at", "started_at", "finished_at", "recorded_at")
        ]
        if not all(times) or times != sorted(times):
            raise ValueError("missing/reversed native timing")
        cutoff = stamp(study["created_at"])
        # Kubernetes native creation timestamps have whole-second resolution.
        if (times[0] - cutoff).total_seconds() < -1:
            raise ValueError("native attempt predates its study")


def audit(root):
    evidence = root / "docs/evidence"
    values, provenance = {}, []
    for key, name in INPUTS.items():
        path = evidence / name
        raw = path.read_bytes()
        values[key] = json.loads(raw)
        provenance.append(
            {
                "role": key,
                "path": str(path.relative_to(root)),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    if values["policy"]["plan"] != values["policy_plan"]:
        raise ValueError("policy capture differs from frozen plan")
    audit_policy_envelopes(values["policy"])
    results = {
        "cold_start_contract": audit_contract(values["contract"]),
        "active_profiling": audit_policy(values["policy"], predecessor=values["predecessor"]),
        "operational_cost": audit_operational(values["operational"], values["operational_plan"]),
        "transfer": audit_transfer(values["transfer"]),
        "drift": audit_drift(values["drift"]),
        "hailo": audit_hailo(values["hailo"], values["hailo_inputs"]),
    }
    return {
        "schema_version": "right-sizing-baseline-audit-v1",
        "evidence_kind": "offline_audit_of_preserved_hardware_records",
        "inputs": provenance,
        "results": results,
        "new_hardware_jobs": 0,
        "overall_goal_complete": False,
        "limitations": [
            "This rechecks captured evidence, not current private scheduler/storage readbacks.",
            "Per-domain duplicate/leakage/digest/quality/cost/budget checks are reused; no timings are copied from Markdown.",
            "Existing successful hardware records are not new experiments or cross-accelerator comparability proof.",
            "Missing live heterogeneous profiling and Slurm feedback qualification remain open.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.root.resolve())
    with args.output.open("x") as target:
        json.dump(result, target, indent=2)
        target.write("\n")
    print("PASS: six preserved evidence domains; zero new hardware Jobs; overall goal remains open")


if __name__ == "__main__":
    main()
