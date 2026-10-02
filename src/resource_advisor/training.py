"""Qualified training isolation: immutable inputs, per-attempt copies, no promotion."""

import copy
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Literal

from pydantic import Field

from .contracts import Contract, Digest, Ref, signature


class TrainingIsolation(Contract):
    ref: Ref
    project_ref: Ref
    workload_ref: Ref
    workload_digest: Digest
    checkpoint_digest: Digest
    input_digest: Digest
    checkpoint_size_bytes: int = Field(gt=0, le=1048576)
    input_size_bytes: int = Field(gt=0, le=1048576)
    validation_refs: tuple[Ref, ...] = Field(min_length=1)
    backend: Literal["kubernetes"] = "kubernetes"
    output_format: Literal["bounded-json-v1"] = "bounded-json-v1"
    auto_promote: Literal[False] = False


def validate_binding(binding, spec, candidate, variant):
    binding = TrainingIsolation.model_validate(binding)
    if (
        binding.ref != spec.ref
        or binding.workload_ref != spec.ref
        or binding.project_ref != spec.project_ref
        or binding.workload_digest != signature(spec)
        or binding.checkpoint_digest != spec.profiling.checkpoint_digest
        or binding.checkpoint_digest != spec.identity.model_digest
        or binding.input_digest != spec.identity.dataset_version
        or variant.verification != "TRAINING_VERIFIED"
        or variant.device_class != "gpu"
        or candidate.backend != binding.backend
        or spec.identity.task_type != "training"
    ):
        raise ValueError("training isolation does not match qualified workload/checkpoint/input")
    if set(spec.profiling.mutable_parameters) - {"host_cpu", "host_memory_mib"}:
        raise ValueError("training semantic parameter search is not qualified")
    if any(c.context.parameters for c in spec.candidates):
        raise ValueError("training runtime parameter overrides are not qualified")
    return binding


class TrainingReceipt(Contract):
    job_id: Ref
    attempt_id: Ref
    result_digest: Digest
    initial_checkpoint_digest: Digest
    input_digest: Digest
    checkpoint_after_digest: Digest
    input_after_digest: Digest
    checkpoint_readonly: Literal[True]
    input_readonly: Literal[True]
    source_not_mounted: Literal[True]
    output_checkpoint_digest: Digest
    output_checkpoint: dict


def validate_receipt(receipt, result, binding):
    receipt = TrainingReceipt.model_validate(receipt)
    binding = TrainingIsolation.model_validate(binding)
    encoded = json.dumps(
        receipt.output_checkpoint,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    if len(encoded) > 16384:
        raise ValueError("checkpoint output exceeds bounded JSON contract")
    if (
        receipt.job_id != result.job_id
        or receipt.attempt_id != result.attempt_id
        or receipt.result_digest != signature(result)
        or receipt.initial_checkpoint_digest != binding.checkpoint_digest
        or receipt.checkpoint_after_digest != binding.checkpoint_digest
        or receipt.input_digest != binding.input_digest
        or receipt.input_after_digest != binding.input_digest
        or receipt.output_checkpoint_digest != signature(receipt.output_checkpoint)
        or result.outcome != "COMPLETED"
    ):
        raise ValueError("training receipt mismatch or input mutation")
    return receipt


def isolate_manifest(manifest, job, sources):
    binding = TrainingIsolation.model_validate(job["body"]["training_isolation"])
    route = sources.get(binding.ref)
    if not route or route.get("binding_digest") != signature(binding):
        raise ValueError("operator training source binding is missing or changed")
    if set(route) != {"binding_digest", "source", "checkpoint_key", "input_key"}:
        raise ValueError("invalid training source configuration")
    source = route["source"]
    if len(source) != 1 or next(iter(source)) not in {"config_map", "pvc"}:
        raise ValueError("training source requires one ConfigMap or PVC")
    for value in [*source.values(), route["checkpoint_key"], route["input_key"]]:
        if not isinstance(value, str) or not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}", value
        ):
            raise ValueError("training source names must be single safe path components")
    if route["checkpoint_key"] == route["input_key"]:
        raise ValueError("checkpoint and input must be separate files")
    pod = manifest["spec"]["template"]["spec"]
    main = pod["containers"][0]
    pod["securityContext"].update(runAsNonRoot=True, runAsUser=1000, runAsGroup=1000, fsGroup=1000)
    main["securityContext"]["readOnlyRootFilesystem"] = True
    volumes = pod.setdefault("volumes", [])
    volumes.extend(
        [
            {
                "name": "training-original",
                **(
                    {"configMap": {"name": source["config_map"]}}
                    if "config_map" in source
                    else {"persistentVolumeClaim": {"claimName": source["pvc"], "readOnly": True}}
                ),
            },
            {"name": "training-copy", "emptyDir": {"sizeLimit": "16Mi"}},
            {"name": "training-tmp", "emptyDir": {"sizeLimit": "64Mi"}},
        ]
    )
    env = [
        {"name": "RA_TRAINING_BINDING", "value": json.dumps(binding.model_dump(mode="json"))},
        {"name": "HOME", "value": "/tmp"},
        {"name": "XDG_CACHE_HOME", "value": "/tmp/cache"},
        {"name": "CUBLAS_WORKSPACE_CONFIG", "value": ":4096:8"},
    ]
    main["env"].extend(env)
    init = {
        "name": "training-copy",
        "image": main["image"],
        "command": ["python", "-m", "resource_advisor.training"],
        "env": copy.deepcopy(main["env"]),
        "securityContext": copy.deepcopy(main["securityContext"]),
        "resources": {
            "requests": {"cpu": "100m", "memory": "128Mi"},
            "limits": {"cpu": "1", "memory": "256Mi"},
        },
        "volumeMounts": copy.deepcopy(main.get("volumeMounts", []))
        + [
            {
                "name": "training-original",
                "mountPath": "/ra-source/checkpoint.json",
                "subPath": route["checkpoint_key"],
                "readOnly": True,
            },
            {
                "name": "training-original",
                "mountPath": "/ra-source/input.json",
                "subPath": route["input_key"],
                "readOnly": True,
            },
            {"name": "training-copy", "mountPath": "/ra-work"},
            {"name": "training-tmp", "mountPath": "/tmp"},
        ],
    }
    main.setdefault("volumeMounts", []).extend(
        [
            {
                "name": "training-copy",
                "mountPath": "/ra-checkpoint",
                "subPath": "checkpoint",
                "readOnly": True,
            },
            {
                "name": "training-copy",
                "mountPath": "/ra-input",
                "subPath": "input",
                "readOnly": True,
            },
            {"name": "training-copy", "mountPath": "/ra-output", "subPath": "output"},
            {"name": "training-tmp", "mountPath": "/tmp"},
        ]
    )
    pod["initContainers"] = [init]
    return manifest


def verified_bytes(path, digest, size):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise ValueError("regular source file required")
        data = file.read(size + 1)
    if len(data) != size or "sha256:" + hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("checkpoint/input size or digest mismatch")
    return data


def prepare(binding, source=Path("/ra-source"), work=Path("/ra-work")):
    binding = TrainingIsolation.model_validate(binding)
    # Verify both sources before writing either copy. Restart never reuses a dirty sandbox.
    inputs = [
        ("checkpoint", binding.checkpoint_digest, binding.checkpoint_size_bytes),
        ("input", binding.input_digest, binding.input_size_bytes),
    ]
    data = {
        kind: verified_bytes(source / (kind + ".json"), digest, size)
        for kind, digest, size in inputs
    }
    for kind in ("checkpoint", "input", "output"):
        (work / kind).mkdir(mode=0o700, exist_ok=False)
    for kind, digest, size in inputs:
        dest = work / kind / (kind + ".json")
        with dest.open("xb") as file:
            file.write(data[kind])
        dest.chmod(0o400)
        verified_bytes(dest, digest, size)
        verified_bytes(source / (kind + ".json"), digest, size)
    print(
        "RA_TRAINING_COPY "
        + json.dumps(
            {
                "checkpoint_digest": binding.checkpoint_digest,
                "input_digest": binding.input_digest,
                "verified_separate_copies": True,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    prepare(json.loads(os.environ["RA_TRAINING_BINDING"]))
