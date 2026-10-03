"""Build negative API cases from independently qualified workload contracts.

These descriptors deliberately violate the old qualification scope. They do not
grant execution support to the changed model, input, architecture or runtime.
"""

from copy import deepcopy


def alias(workload, variant, ref):
    workload, variant = deepcopy(workload), deepcopy(variant)
    candidate = next(c for c in workload["candidates"] if c["variant_ref"] == variant["ref"])
    workload.update(ref=ref, candidates=[candidate], baseline_candidate_ref=candidate["ref"])
    variant.update(ref="v-" + ref, workload_ref=ref)
    candidate["variant_ref"] = variant["ref"]
    return workload, variant


def scope_cases(workload, variant, prefix):
    cases = []
    for fault, reasons in [
        ("shape", ["LOGICAL_WORKLOAD_MISMATCH", "INPUT_SHAPE_UNVERIFIED"]),
        ("batch", ["LOGICAL_WORKLOAD_MISMATCH", "INPUT_SHAPE_UNVERIFIED"]),
        ("runtime", ["RUNTIME_MISMATCH"]),
        ("architecture", ["ARCH_MISMATCH"]),
        ("precision", ["LOGICAL_WORKLOAD_MISMATCH", "PRECISION_MISMATCH"]),
    ]:
        spec, runtime = alias(workload, variant, prefix + "-" + fault)
        context = spec["candidates"][0]["context"]
        if fault == "shape":
            spec["identity"]["input_shape"][-1] += 16
        elif fault == "batch":
            spec["identity"]["input_shape"][0] *= 2
            spec["identity"]["batch_size"] *= 2
        elif fault == "runtime":
            context["runtime_versions"]["pytorch"] += "-unqualified"
        elif fault == "architecture":
            context["arch"] = "arm64" if context["arch"] == "amd64" else "amd64"
        else:
            spec["identity"]["precision"] = (
                "fp16" if spec["identity"]["precision"] == "fp32" else "fp32"
            )
        cases.append({"case": fault, "workload": spec, "variant": runtime, "reasons": reasons})
    return cases


def budget_cases(workload, variant, prefix):
    cases = []
    for fault, reason in [
        ("no-consent", "explicit profiling consent is required"),
        (
            "short-confirmation",
            "final budget must cover independent baseline and finalist confirmation",
        ),
        ("no-exploration-budget", "total budget must leave a probe budget after final reservation"),
        ("no-device-budget", "EXPLICIT_DEVICE_BUDGET_REQUIRED"),
    ]:
        spec, runtime = alias(workload, variant, prefix + "-" + fault)
        policy = spec["profiling"]
        policy["consent"] = fault != "no-consent"
        if fault == "short-confirmation":
            policy["final_validation_seconds"] = 1
        elif fault == "no-exploration-budget":
            policy["total_wall_seconds"] = policy["final_validation_seconds"]
        elif fault == "no-device-budget":
            policy["device_seconds"] = {}
        cases.append({"case": fault, "workload": spec, "variant": runtime, "reason": reason})
    return cases
