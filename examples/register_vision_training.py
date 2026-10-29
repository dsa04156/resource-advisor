"""Build one vision-training workload with every qualified single-GPU candidate.

One model/dataset/global-batch/steps/optimizer setting is one workload; its
candidates are the devices where a direct qualification Job PASSed with the same
runtime image family. All candidates start from the same pinned initial state
(tensor digest), so their measurements describe the same training problem.
Variants are one per image (architecture). `--post` registers through the API.

    python examples/register_vision_training.py --plan PLAN.json --dataset DIR --out OUT \
        [--post --api-dir DIR_WITH_api.py]
"""

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from resource_advisor import vision_training
from resource_advisor.contracts import (
    Candidate,
    CapabilitySnapshot,
    DistributedNode,
    DistributedTraining,
    ExecutionContext,
    ExecutionPolicy,
    ProfilingPolicy,
    QualityPolicy,
    Resources,
    RuntimeVariant,
    WorkloadIdentity,
    WorkloadSpec,
    signature,
)
from resource_advisor.training import TrainingIsolation

PROJECT = "ra-lab"


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def environment_of(image):
    return "sha256:" + image.rsplit("@sha256:", 1)[1]


def build(plan, dataset, observed_at):
    manifest = json.loads((Path(dataset) / "manifest.json").read_text())
    if signature(manifest) != plan["dataset_digest"]:
        raise SystemExit("dataset differs from the planned manifest")
    ref = plan["ref"]
    init = vision_training.init_descriptor(
        plan["model"], plan["seed"], len(manifest["classes"]), plan["initial_state_digest"]
    )
    cp, inp = encode(init), encode(manifest)
    cp_digest = "sha256:" + hashlib.sha256(cp).hexdigest()
    inp_digest = "sha256:" + hashlib.sha256(inp).hexdigest()
    batch, steps = plan["global_batch"], plan["steps"]
    # A distributed plan (one DDP candidate over plan["candidates"] as its member nodes)
    # signs the per-rank micro batch as the input shape and the fixed global batch.
    ddp = plan.get("distributed")
    world = len(plan["candidates"]) if ddp else 1
    micro = batch // world
    # A plan may name another entry (e.g. the joint KD runner) and its extra signed flags.
    command = (
        *plan.get("entry", ("python3", "/opt/ra-train/vision_training.py")), "--isolated",
        "--model", plan["model"], "--data", "/opt/ra-train/data",
        "--dataset-digest", plan["dataset_digest"],
        "--initial-state", plan["initial_state_path"],
        "--initial-checkpoint-digest", plan["initial_state_digest"],
        "--global-batch", str(batch), "--steps", str(steps),
        "--lr", str(plan["lr"]), "--weight-decay", str(plan["weight_decay"]),
        "--seed", str(plan["seed"]),
        *(("--strategy", "ddp") if ddp else ()),
        *plan.get("extra_args", ()),
    )
    quality = QualityPolicy(
        metric="holdout_top1", minimum=0.0, minimum_repeats=1,
        maximum_peak_memory_mib=float(plan["host_memory_mib"]),
    )
    identity = WorkloadIdentity(
        code_digest=plan["code_digest"],
        model_digest=cp_digest,
        dataset_version=inp_digest,
        config_digest=plan.get("config_digest") or signature({"command": command}),
        task_type="training",
        input_shape=(micro, 3, 224, 224),
        batch_size=micro,
        global_batch_size=batch,
        precision="fp32",
        seed=plan["seed"],
        work_units=steps,
        quality_contract_digest=signature(quality),
        measurement_boundary=plan.get("measurement_boundary", vision_training.BOUNDARY),
        model_name=plan["model"],
        optimizer="adamw",
        optimizer_parameters={"lr": plan["lr"], "weight_decay": plan["weight_decay"]},
    )
    variants, capabilities, candidates = {}, [], []
    for c in plan["candidates"]:
        environment = environment_of(c["image"])
        vref = f"{ref}-{c['arch']}"
        variants.setdefault(vref, {"c": c, "validation": []})["validation"].append(
            c["validation_ref"]
        )
        capability = CapabilitySnapshot(
            ref=f"{ref}-{c['candidate']}-cap",
            node_ref=c["node"],
            backend_cluster_id=c["cluster"],
            backend="kubernetes",
            observed_at=observed_at,
            valid_for_seconds=43200,
            ready=True,
            arch=c["arch"],
            accelerator_vendor="nvidia",
            accelerator_model=c["accelerator_model"],
            device_class="gpu",
            allocation_mode=c["allocation_mode"],
            allocatable_count=1,
            host_cpu=c["node_cpu"],
            host_memory_mib=c["node_memory_mib"],
            resource_key=c["resource_key"],
            runtime_versions=c["runtime_versions"],
            environment_digest=environment,
            memory_model=c["memory_model"],
            power_mode="default",
            # The qualification Job observed this device through the device-plugin
            # allocation; a shared slot is still not isolated.
            reservation_verified=True,
            isolation_verified=False,
            evidence_refs=(c["validation_ref"],),
        )
        capabilities.append(capability)
        if ddp:
            continue
        candidates.append(
            Candidate(
                ref=c["candidate"],
                variant_ref=vref,
                capability_ref=capability.ref,
                backend="kubernetes",
                context=ExecutionContext(
                    arch=c["arch"],
                    environment_digest=environment,
                    runtime_versions=c["runtime_versions"],
                    accelerator_model=c["accelerator_model"],
                    memory_model=c["memory_model"],
                    power_mode="default",
                    allocation_mode=c["allocation_mode"],
                    resources=Resources(
                        host_cpu=plan["host_cpu"],
                        host_memory_mib=plan["host_memory_mib"],
                        accelerator_count=1,
                    ),
                ),
            )
        )
    if ddp:
        first = plan["candidates"][0]
        distributed = DistributedTraining(
            strategy="ddp",
            launcher="torchrun",
            collective=ddp["collective"],
            nodes=tuple(
                sorted(
                    (
                        DistributedNode(
                            node_ref=c["node"],
                            capability_ref=cap.ref,
                            gpus=1,
                            accelerator_model=c["accelerator_model"],
                            validation_ref=c["validation_ref"],
                        )
                        for c, cap in zip(plan["candidates"], capabilities, strict=True)
                    ),
                    key=lambda n: n.node_ref,
                )
            ),
            nproc_per_node=1,
            world_size=world,
        )
        candidates.append(
            Candidate(
                ref=ddp["candidate"],
                variant_ref=f"{ref}-{first['arch']}",
                capability_ref=capabilities[0].ref,
                backend="kubernetes",
                context=ExecutionContext(
                    arch=first["arch"],
                    environment_digest=environment_of(first["image"]),
                    runtime_versions=first["runtime_versions"],
                    accelerator_model=first["accelerator_model"],
                    memory_model=first["memory_model"],
                    power_mode="default",
                    allocation_mode="physical_device",
                    resources=Resources(
                        host_cpu=plan["host_cpu"],
                        host_memory_mib=plan["host_memory_mib"],
                        accelerator_count=1,
                    ),
                    distributed=distributed,
                ),
            )
        )
    variant_bodies = [
        RuntimeVariant(
            ref=vref,
            workload_ref=ref,
            project_ref=PROJECT,
            workload_signature=signature(identity),
            arch=v["c"]["arch"],
            accelerator_vendor="nvidia",
            device_class="gpu",
            precision="fp32",
            model_digest=cp_digest,
            image=v["c"]["image"],
            environment_digest=environment_of(v["c"]["image"]),
            command=command,
            verification="TRAINING_VERIFIED",
            validation_refs=tuple(v["validation"]) + tuple(plan.get("extra_validation_refs", ())),
            supported_shapes=((micro, 3, 224, 224),),
            runtime_versions=v["c"]["runtime_versions"],
            distributed_launch="torchrun-ddp" if ddp else None,
        )
        for vref, v in variants.items()
    ]
    spec = WorkloadSpec(
        ref=ref,
        project_ref=PROJECT,
        identity=identity,
        candidates=tuple(candidates),
        baseline_candidate_ref=candidates[0].ref,
        quality=quality,
        execution=ExecutionPolicy(max_run_seconds=900, max_queue_seconds=600),
        profiling=ProfilingPolicy(checkpoint_digest=cp_digest),
    )
    binding = TrainingIsolation(
        ref=ref,
        project_ref=PROJECT,
        workload_ref=ref,
        workload_digest=signature(spec),
        checkpoint_digest=cp_digest,
        input_digest=inp_digest,
        checkpoint_size_bytes=len(cp),
        input_size_bytes=len(inp),
        validation_refs=tuple(c["validation_ref"] for c in plan["candidates"])
        + tuple(plan.get("extra_validation_refs", ())),
    )
    return {
        "capabilities": [c.model_dump(mode="json") for c in capabilities],
        "variants": [v.model_dump(mode="json") for v in variant_bodies],
        "workload": spec.model_dump(mode="json"),
        "training_isolation": binding.model_dump(mode="json"),
        "binding_digest": signature(binding),
        "files": {"checkpoint.json": cp.decode(), "input.json": inp.decode()},
    }


def post(built, call):
    for kind, path, bodies in (
        ("capability", "/capabilities", built["capabilities"]),
        ("variant", "/variants", built["variants"]),
        ("workload", "/workloads", [built["workload"]]),
        ("training_isolation", "/training-isolation", [built["training_isolation"]]),
    ):
        for body in bodies:
            status, detail = call("POST", "/api/v1/compute" + path, body)
            print(kind, body["ref"], status, "" if status == 200 else json.dumps(detail)[:300])
            if status != 200:
                raise SystemExit(1)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--plan", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--post", action="store_true")
    p.add_argument("--api-dir", help="directory holding the token-reading api.py helper")
    args = p.parse_args(argv)
    built = build(json.loads(Path(args.plan).read_text()), args.dataset, datetime.now(UTC))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, value in built["files"].items():
        (out / name).write_text(value)
    (out / "registration.json").write_text(
        json.dumps({k: v for k, v in built.items() if k != "files"}, indent=1)
    )
    print(json.dumps({"binding_digest": built["binding_digest"],
                      "workload_signature": built["variants"][0]["workload_signature"],
                      "model_digest": built["variants"][0]["model_digest"]}))
    if args.post:
        sys.path.insert(0, args.api_dir)
        import api

        post(built, api.call)


if __name__ == "__main__":
    main()
