from datetime import timedelta

import pytest
from pydantic import ValidationError

from resource_advisor.contracts import ExecutionResult, RuntimeVariant, WorkloadSpec, signature
from resource_advisor.policy import compatibility, context_signature


def test_two_signatures_separate_environment(bundle):
    spec, candidate, variant, _ = bundle
    moved = candidate.model_copy(
        update={"context": candidate.context.model_copy(update={"power_mode": "other"})}
    )
    assert context_signature(moved, variant) != context_signature(candidate, variant)
    assert signature(spec.identity) == signature(
        spec.model_copy(update={"candidates": (moved,)}).identity
    )


def test_explicit_runtime_bundle_is_signed_and_legacy_digest_is_preserved(bundle):
    _, candidate, variant, _ = bundle
    legacy = variant.model_dump(mode="json")
    assert "kubernetes_runtime_bundle_ref" not in legacy
    explicit_none = RuntimeVariant.model_validate({**legacy, "kubernetes_runtime_bundle_ref": None})
    assert signature(explicit_none) == signature(variant)
    attached = RuntimeVariant.model_validate(
        {**legacy, "kubernetes_runtime_bundle_ref": "approved-runtime"}
    )
    assert context_signature(candidate, attached) != context_signature(candidate, variant)
    with pytest.raises(ValidationError, match="pinned container"):
        RuntimeVariant.model_validate({**attached.model_dump(mode="json"), "image": None})
    _, _, _, cap = bundle
    assert "KUBERNETES_RUNTIME_BINDING_ON_SLURM" in compatibility(
        bundle[0],
        candidate.model_copy(update={"backend": "slurm"}),
        attached,
        cap.model_copy(update={"backend": "slurm"}),
    )


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("ready", False, "NODE_UNAVAILABLE"),
        ("allocatable_count", 0, "INSUFFICIENT_CAPACITY"),
        ("environment_digest", signature("changed"), "ENVIRONMENT_MISMATCH"),
        ("reservation_verified", False, "RESERVATION_UNVERIFIED"),
        ("arch", "amd64", "ARCH_MISMATCH"),
    ],
)
def test_fail_closed_capabilities(bundle, field, value, error):
    spec, candidate, variant, cap = bundle
    assert error in compatibility(spec, candidate, variant, cap.model_copy(update={field: value}))


def test_expired_inventory(bundle):
    spec, candidate, variant, cap = bundle
    assert "CAPABILITY_STALE" in compatibility(
        spec, candidate, variant, cap, cap.observed_at + timedelta(seconds=181)
    )


def test_detected_does_not_mean_model_verified(bundle):
    spec, candidate, variant, cap = bundle
    variant = variant.model_copy(update={"verification": "DETECTED", "validation_refs": ()})
    assert "MODEL_UNVERIFIED" in compatibility(spec, candidate, variant, cap)


def test_gpu_count_search_rejected(bundle):
    spec, candidate, _, _ = bundle
    data = spec.model_dump(mode="json")
    other = candidate.model_dump(mode="json")
    other["ref"] = "other"
    other["context"]["resources"]["accelerator_count"] = 2
    data["candidates"].append(other)
    with pytest.raises(ValidationError, match="accelerator count"):
        WorkloadSpec.model_validate(data)


def test_prediction_is_not_result():
    with pytest.raises(ValidationError):
        ExecutionResult.model_validate({"measured": False})
