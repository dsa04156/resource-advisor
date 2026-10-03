"""Recheck published live API boundary evidence without contacting infrastructure."""

import argparse
import json
from datetime import datetime
from pathlib import Path

from contract_boundary_cases import budget_cases, scope_cases

from resource_advisor.contracts import ExecutionResult, RuntimeVariant, WorkloadSpec, signature
from resource_advisor.diagnostics import validate_profile
from resource_advisor.policy import context_signature
from resource_advisor.thermal import assess, validate_trace


def require(condition, message):
    if not condition:
        raise ValueError(message)


def audit(report):
    require(
        report["schema_version"] == "contract-boundaries-v1"
        and report["evidence_kind"] == "live-api-and-gpu-qualification",
        "wrong evidence type",
    )
    spec = WorkloadSpec.model_validate(report["workload"])
    variant = RuntimeVariant.model_validate(report["variant"])
    source = WorkloadSpec.model_validate(report["source_workload"])
    qualification = report["qualification"]
    result = ExecutionResult.model_validate(qualification["result"])
    require(
        signature(result) == qualification["result_digest"]
        and result.workload_signature == signature(spec.identity)
        and result.evidence_kind == "hardware"
        and result.measured
        and result.outcome == "COMPLETED"
        and result.job_id == qualification["job_ref"]
        and qualification["exit_code"] == 0
        and result.measurements.quality_value >= spec.quality.minimum
        and result.measurements.peak_memory_mib <= spec.quality.maximum_peak_memory_mib
        and result.measurements.work_units == spec.identity.work_units
        and variant.workload_signature == signature(spec.identity)
        and variant.model_digest == spec.identity.model_digest
        and qualification["fixture"]["model_weights_digest"] == spec.identity.model_digest
        and qualification["fixture"]["seed"] == spec.identity.seed
        and qualification["fixture"]["shape"] == list(spec.identity.input_shape),
        "qualification identity or result mismatch",
    )
    start, end = (
        datetime.fromisoformat(qualification[key]) for key in ("scheduled_at", "finished_at")
    )
    require(
        start.tzinfo is not None
        and end.tzinfo is not None
        and 0 < (end - start).total_seconds() == qualification["gpu_reservation_seconds"] <= 90,
        "qualification cost mismatch",
    )
    validate_profile(qualification["phase_profile"], result, spec.identity.measurement_boundary)
    trace = validate_trace(
        qualification["thermal_trace"],
        result,
        {
            "spec": report["workload"],
            "variant": report["variant"],
            "candidate": spec.candidates[0].model_dump(mode="json"),
        },
    )
    require(
        assess(trace, variant.thermal_policy) == qualification["thermal_assessment"]
        and qualification["thermal_assessment"]["status"] == "ELIGIBLE_TRACE",
        "thermal evidence mismatch",
    )
    responses = report["responses"]

    def response(key, code, path, request):
        row = responses[key]
        require(
            row["status_code"] == code
            and row["method"] == "POST"
            and row["path"] == path
            and row["request"] == request,
            "request/response mismatch: " + key,
        )
        return row["response"]

    def registration(key, workload, runtime):
        saved_workload = response(key + "-workload", 200, "/workloads", workload)
        saved_variant = response(key + "-variant", 200, "/variants", runtime)
        require(
            WorkloadSpec.model_validate(saved_workload) == WorkloadSpec.model_validate(workload)
            and RuntimeVariant.model_validate(saved_variant)
            == RuntimeVariant.model_validate(runtime),
            "registered contract mismatch",
        )

    def recommendation(key, workload, expected):
        value = response(key, 200, "/recommendations", {"workload_ref": workload.ref})
        require(
            value["workload_digest"] == signature(workload)
            and value["digest"] == signature({k: v for k, v in value.items() if k != "digest"})
            and value["status"] == expected
            and not value["measured"]
            and value["candidate_ref"] is None
            and value["ranking"] == [],
            "unsafe recommendation: " + key,
        )
        return value

    registration("fresh", report["workload"], report["variant"])
    empty = recommendation("fresh-recommendation", spec, "NEEDS_PROFILE")
    require(empty["excluded"] == {"cpu1": ["NEEDS_PROFILE"]}, "wrong abstention reason")
    response(
        "no-evidence-approval",
        422,
        "/recommendations/" + empty["ref"] + "/approve",
        {"candidate_ref": "cpu1", "recommendation_digest": empty["digest"]},
    )
    pilot = response(
        "unreserved-pilot",
        422,
        "/jobs",
        {"workload_ref": spec.ref, "candidate_ref": "cpu1", "mode": "pilot"},
    )
    require("reserved study plan" in pilot["detail"], "wrong pilot denial")
    budgets = budget_cases(report["workload"], report["variant"], spec.ref)
    require(report["budget_cases"] == budgets, "budget case design changed")
    for case in budgets:
        registration(case["case"], case["workload"], case["variant"])
        value = response(
            case["case"] + "-study",
            422,
            "/profiling-runs",
            {"workload_ref": case["workload"]["ref"], "strategy": "qlognei", "seed": 20261005},
        )
        require(case["reason"] in value["detail"], "wrong budget rejection")
    control = response(
        "existing-history-control", 200, "/recommendations", {"workload_ref": source.ref}
    )
    require(control["measured"] and control["ranking"], "missing measured positive control")
    support = {ref for rank in control["ranking"] for ref in rank["evidence_refs"]}
    profiles = report["control_support_profiles"]
    require(
        len(profiles) == len(support) and {p["result"]["attempt_id"] for p in profiles} == support,
        "missing control evidence",
    )
    require(signature(spec.identity) != signature(source.identity), "new workload is not new")
    for profile in profiles:
        saved = ExecutionResult.model_validate(profile["result"])
        require(
            saved.evidence_kind == "hardware"
            and saved.measured
            and saved.outcome == "COMPLETED"
            and saved.workload_signature == signature(source.identity),
            "invalid historical control result",
        )
        require(
            datetime.fromisoformat(profile["recorded_at"])
            < datetime.fromisoformat(control["created_at"]),
            "future control evidence",
        )
    scopes = scope_cases(report["source_workload"], report["source_variant"], spec.ref)
    require(report["scope_cases"] == scopes, "scope case design changed")
    source_candidate = next(c for c in source.candidates if c.ref == "cpu1")
    original_context = context_signature(
        source_candidate, RuntimeVariant.model_validate(report["source_variant"])
    )
    for case in scopes:
        registration(case["case"], case["workload"], case["variant"])
        changed = WorkloadSpec.model_validate(case["workload"])
        value = recommendation(case["case"] + "-recommendation", changed, "NO_COMPATIBLE_VARIANT")
        require(set(case["reasons"]) <= set(value["excluded"]["cpu1"]), "wrong scope rejection")
        value = response(
            case["case"] + "-submission",
            422,
            "/jobs",
            {"workload_ref": changed.ref, "candidate_ref": "cpu1", "mode": "observe"},
        )
        require(
            all(reason in value["detail"] for reason in case["reasons"]),
            "wrong submission rejection",
        )
        context = context_signature(
            changed.candidates[0], RuntimeVariant.model_validate(case["variant"])
        )
        require(
            signature(changed.identity) != signature(source.identity)
            if case["case"] in {"shape", "batch", "precision"}
            else context != original_context,
            "changed scope not separated",
        )
    preservation = report["preservation"]
    require(
        preservation["database_before"] == preservation["database_after"]
        and preservation["objects_before"] == preservation["objects_after"],
        "side effects observed",
    )
    require(
        all(
            preservation[k] == 0
            for k in [
                "api_jobs_created",
                "studies_created",
                "usage_records_created",
                "f0_results_in_profiles",
            ]
        ),
        "unexpected execution/profile writes",
    )
    require(
        preservation["queue_idle"]
        and preservation["stable_entities_unchanged"]
        and all(preservation["objects_unchanged"].values()),
        "preservation failed",
    )
    return {
        "scope_cases": len(scopes),
        "budget_cases": len(budgets),
        "control_profiles": len(profiles),
        "api_jobs_created": 0,
        "qualification_gpu_seconds": qualification["gpu_reservation_seconds"],
        "capture_digest": signature(report),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(json.loads(args.capture.read_text())), indent=2))
