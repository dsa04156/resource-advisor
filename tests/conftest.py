import os

import pytest

from resource_advisor.contracts import (
    Candidate,
    CapabilitySnapshot,
    ExecutionContext,
    QualityPolicy,
    Resources,
    RuntimeVariant,
    WorkloadIdentity,
    WorkloadSpec,
    now,
    signature,
)
from resource_advisor.service import Service
from resource_advisor.store import Store, metadata


@pytest.fixture
def bundle():
    quality = QualityPolicy(minimum=0.9, maximum_peak_memory_mib=1024)
    digest = signature("synthetic-contract-fixture")
    resources = Resources(host_cpu=1, host_memory_mib=512, accelerator_count=1)
    context = ExecutionContext(
        arch="arm64",
        environment_digest=digest,
        runtime_versions={"cuda": "fixture"},
        accelerator_model="fixture-gpu",
        memory_model="unified",
        power_mode="fixture",
        allocation_mode="physical_device",
        resources=resources,
    )
    cap = CapabilitySnapshot(
        ref="cap-1",
        node_ref="test-worker",
        backend_cluster_id="lab",
        backend="kubernetes",
        observed_at=now(),
        ready=True,
        arch="arm64",
        accelerator_vendor="nvidia",
        accelerator_model="fixture-gpu",
        device_class="gpu",
        allocation_mode="physical_device",
        allocatable_count=1,
        host_cpu=4,
        host_memory_mib=8192,
        resource_key="nvidia.com/gpu",
        runtime_versions=context.runtime_versions,
        environment_digest=digest,
        memory_model="unified",
        power_mode="fixture",
        reservation_verified=True,
        evidence_refs=("fixture-reservation",),
    )
    identity = WorkloadIdentity(
        code_digest=digest,
        model_digest=digest,
        dataset_version="fixture-v1",
        config_digest=digest,
        task_type="inference",
        input_shape=(1, 3, 32, 32),
        batch_size=1,
        precision="fp32",
        work_units=10,
        quality_contract_digest=signature(quality),
        measurement_boundary="fixture-forward-only",
    )
    variant = RuntimeVariant(
        ref="variant-1",
        workload_ref="workload-1",
        project_ref="team-a",
        workload_signature=signature(identity),
        arch="arm64",
        accelerator_vendor="nvidia",
        device_class="gpu",
        precision="fp32",
        model_digest=digest,
        image="example.invalid/runner@" + digest,
        environment_digest=digest,
        command=("python", "workload.py"),
        verification="MODEL_VERIFIED",
        validation_refs=("fixture-validation",),
        supported_shapes=((1, 3, 32, 32),),
        runtime_versions=context.runtime_versions,
    )
    candidate = Candidate(
        ref="base",
        variant_ref=variant.ref,
        capability_ref=cap.ref,
        backend="kubernetes",
        context=context,
    )
    spec = WorkloadSpec(
        ref="workload-1",
        project_ref="team-a",
        identity=identity,
        candidates=(candidate,),
        baseline_candidate_ref="base",
        quality=quality,
    )
    return spec, candidate, variant, cap


@pytest.fixture
def service(bundle):
    url = os.getenv("RA_TEST_DATABASE_URL", "sqlite://")
    store = Store(url)
    if url != "sqlite://":
        if not (store.engine.url.database or "").startswith("ra_test_"):
            raise ValueError(
                "destructive test reset is allowed only in a dedicated ra_test_* database"
            )
        metadata.drop_all(store.engine)
    store.initialize()
    svc = Service(store, accept_synthetic=True)
    spec, _, variant, cap = bundle
    for kind, model in [("workload", spec), ("variant", variant), ("capability", cap)]:
        svc.register(kind, model, "team-a")
    yield svc
    store.engine.dispose()
