"""TEST DATA ONLY: a local Console API seeded with labelled fixture registrations.

The accelerator names below describe test fixtures, not live inventory. No worker
or native backend is configured, so submitted jobs stay in the scheduler-facing
VALIDATED state in a throwaway SQLite file and never reach hardware.

Preview (auto-connects to the fixture project; production API/DB untouched):
  rtk proxy uv run python examples/workload_console_fixture.py --host 0.0.0.0 --port 18432
"""

import argparse
import hashlib
import tempfile

import uvicorn

from resource_advisor.api import Principal, create_app
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
from resource_advisor.scheduling import SchedulingProfile
from resource_advisor.service import Service
from resource_advisor.store import Store

PROJECT = "TEST-DATA-fixture"
TOKEN = "fixture-console-token"
QUALITY = QualityPolicy(minimum=0.9, maximum_peak_memory_mib=4096)
# (candidate, backend, device, vendor, model, runtime, accelerator count)
DEVICES = {
    "cuda": ("kubernetes", "gpu", "nvidia", "TEST RTX 5080 (fixture)", "cuda", 1),
    "hailo": ("kubernetes", "npu", "hailo", "TEST Hailo-8 (fixture)", "hailort", 1),
    "mobilint": ("kubernetes", "npu", "mobilint", "TEST Mobilint ARIES (fixture)", "mobilint", 1),
    "slurm": ("slurm", "cpu", "none", "TEST Slurm CPU (fixture)", "python", 0),
}


def register_workload(service, ref, task_type, devices, *, cpu=2, memory=2048):
    digest = signature("fixture-" + ref)
    identity = WorkloadIdentity(
        code_digest=digest,
        model_digest=digest,
        dataset_version="fixture-v1",
        config_digest=digest,
        task_type=task_type,
        input_shape=(1, 3, 224, 224),
        batch_size=1,
        precision="fp32",
        work_units=10,
        quality_contract_digest=signature(QUALITY),
        measurement_boundary="fixture-forward-only",
    )
    candidates = []
    for key in devices:
        backend, device, vendor, model, runtime, count = DEVICES[key]
        versions = {runtime: "fixture"}
        if vendor == "mobilint":
            versions["firmware_identity"] = "fixture"
        mode = "cpu_only" if device == "cpu" else "physical_device"
        resources = Resources(host_cpu=cpu, host_memory_mib=memory, accelerator_count=count)
        cap = CapabilitySnapshot(
            ref=f"{ref}-{key}-cap",
            node_ref=f"fixture-{key}-node",
            backend_cluster_id="fixture-slurm" if backend == "slurm" else "fixture-k8s",
            backend=backend,
            observed_at=now(),
            valid_for_seconds=86400,
            ready=True,
            arch="amd64",
            accelerator_vendor=vendor,
            accelerator_model=model,
            device_class=device,
            allocation_mode=mode,
            allocatable_count=count,
            host_cpu=16,
            host_memory_mib=65536,
            resource_key=None if device == "cpu" else f"{vendor}.example/{device}",
            runtime_versions=versions,
            environment_digest=digest,
            memory_model="discrete",
            power_mode="fixture",
            reservation_verified=True,
            evidence_refs=("fixture-reservation",),
        )
        variant = RuntimeVariant(
            ref=f"{ref}-{key}-variant",
            workload_ref=ref,
            project_ref=PROJECT,
            workload_signature=signature(identity),
            arch="amd64",
            accelerator_vendor=vendor,
            device_class=device,
            precision="fp32",
            model_digest=digest,
            image="example.invalid/fixture@" + digest,
            environment_digest=digest,
            command=("python", "fixture.py"),
            verification="TRAINING_VERIFIED" if task_type == "training" else "MODEL_VERIFIED",
            validation_refs=("fixture-validation",),
            supported_shapes=((1, 3, 224, 224),),
            runtime_versions=versions,
        )
        context = ExecutionContext(
            arch="amd64",
            environment_digest=digest,
            runtime_versions=versions,
            accelerator_model=model,
            memory_model="discrete",
            power_mode="fixture",
            allocation_mode=mode,
            resources=resources,
        )
        candidates.append(
            Candidate(
                ref=f"{ref}-{key}",
                variant_ref=variant.ref,
                capability_ref=cap.ref,
                backend=backend,
                context=context,
            )
        )
        service.register("capability", cap, PROJECT)
        service.register("variant", variant, PROJECT)
    spec = WorkloadSpec(
        ref=ref,
        project_ref=PROJECT,
        identity=identity,
        candidates=tuple(candidates),
        baseline_candidate_ref=candidates[0].ref,
        quality=QUALITY,
    )
    service.register("workload", spec, PROJECT)


def profile(ref, name, **policy):
    return SchedulingProfile.model_validate(
        {
            "ref": ref,
            "project_ref": PROJECT,
            "name": name,
            "version": 1,
            "policy": {
                "max_resources": {"host_cpu": 8, "host_memory_mib": 16384, "accelerator_count": 2},
                "max_run_seconds": 600,
                "max_queue_seconds": 900,
                **policy,
            },
            "bindings": [
                {
                    "backend": "kubernetes",
                    "cluster_ref": "fixture-k8s",
                    "namespace": "fixture-namespace",
                    "local_queue": "fixture-queue",
                    "priority_map": {"normal": None},
                },
                {
                    "backend": "slurm",
                    "cluster_ref": "fixture-slurm",
                    "partition": "fixture-partition",
                    "account": "fixture-account",
                    "priority_map": {"normal": "fixture-normal"},
                },
            ],
        }
    )


def build(url="sqlite://", *, catalog=True):
    store = Store(url)
    store.initialize()
    workloads = {
        "fixture-image-classify": ("inference", ["cuda", "hailo", "mobilint"]),
        "fixture-cpu-benchmark": ("benchmark", ["slurm"]),
        # Training has no isolation binding, so the catalog shows why it cannot run.
        "fixture-cnn-training": ("training", ["cuda"]),
    }
    service = Service(
        store,
        accept_synthetic=True,
        operational_mode=True,
        # catalog=False configures a catalog naming no registered workload: an empty catalog.
        console_workloads=tuple(workloads) if catalog else ("fixture-not-registered",),
    )
    for ref, (task_type, devices) in workloads.items():
        register_workload(service, ref, task_type, devices)
    service.register(
        "scheduling_profile",
        profile(
            "fixture-default",
            "기본 정책 (TEST DATA)",
            allocation_modes=["physical_device", "virtual_slot", "cpu_only"],
        ),
        PROJECT,
    )
    # A real policy that rejects accelerator work, used to show a rejected plan.
    service.register(
        "scheduling_profile",
        profile(
            "fixture-cpu-only",
            "CPU 전용 정책 (TEST DATA)",
            allocation_modes=["cpu_only"],
            backend_order=["slurm", "kubernetes"],
        ),
        PROJECT,
    )
    return service


def app(url="sqlite://", *, catalog=True, preview=False):
    service = build(url, catalog=catalog)
    return create_app(
        service,
        {hashlib.sha256(TOKEN.encode()).hexdigest(): Principal(PROJECT)},
        # Preview viewers land in the fixture project without a token; nothing else exists.
        anonymous_project=PROJECT if preview else None,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TEST DATA Console preview")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18432)
    args = parser.parse_args()
    database = tempfile.mkdtemp(prefix="workload-console-preview-") + "/fixture.db"
    print(f"TEST DATA preview: http://{args.host}:{args.port}/console (db {database})")
    uvicorn.run(app("sqlite:///" + database, preview=True), host=args.host, port=args.port)
