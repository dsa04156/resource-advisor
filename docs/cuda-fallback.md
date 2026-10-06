# Live E0 CUDA fallback refusal

The unchanged CUDA matmul runner passed its positive control and refused CPU
fallback when GPU visibility was removed. Two actual Kueue-admitted Jobs followed
the [frozen protocol](cuda-fallback-plan.md), committed before submission. No
replacement Job or API profile was created. [Evidence](evidence/cuda-fallback-v1.json)
retains exact runtime versions, image/source digests, allocation costs and positive
result; private native manifests, logs and identities remain outside Git.

| Check | Allocated positive | Explicitly hidden-GPU negative |
|---|---|---|
| Actual PyTorch / CUDA | 2.8.0+cu128 / 12.8 | Same |
| CUDA available / device count | true / 1 | false / 0 |
| Requested GPU | 1 | None; visibility masked |
| Native Job | Completed | Failed as required |
| Container exit | 0 | 1 |
| Runtime outcome | One validated hardware result, numerical agreement1.0 | `CUDA unavailable; refusing CPU fallback`; no result marker |
| GPU reservation | 2 seconds | 0 seconds |
| CPU core reservation | 2 seconds | 1 second |

Both used the same actual image digest, read-only qualified source/runtime
mounts, FP32 256×256 matmul, seed0, three warmups and 20 timed iterations. A thin
identical wrapper printed actual CUDA visibility before invoking the real
`resource_advisor.gpu_benchmark`; it did not replace the benchmark or manufacture
an exception. The positive result's identity/schema/digest and numerical outputs
passed. Its twenty forwards totaled 0.000267791 seconds; this is distinct from
two reservation seconds and not a saturated-GPU performance measurement.

The negative omitted the GPU resource key and set NVIDIA_VISIBLE_DEVICES=void
and CUDA_VISIBLE_DEVICES empty. Therefore the evidence proves refusal under
**explicit GPU visibility loss**, not protection against a hostile user changing
visibility settings or complete unreserved-device isolation. That distinction is
part of the report. No CPU performance measurement was accepted.

The existing queue performed admission; Jobs began suspended and were never
manually unsuspended. Both Pods terminated with zero restarts, and the queue
returned to zero pending/admitted workloads. Native Job/Pod/Workload evidence is
retained. All preexisting namespace Job/Pod/Workload/Deployment/PVC UIDs and specs
matched their before-state, as did source ConfigMap data. The node was Ready
without pressure before execution. No driver, queue/quota, static service or
runtime dependency was modified.

Independent PostgreSQL snapshots matched API Job/usage/outbox counts and all
2,637 immutable result/profile/tracking/artifact records. Inventory may continue
and the unrelated disconnected Slurm job may update observation-error metadata;
whole-database immutability is not claimed. The two direct qualification Jobs
remain outside application result/profile/MLflow/usage history; all their newly
incurred cost is reported here: **2 GPU-seconds and 3 CPU core-seconds**. Core
reservation is not measured CPU busy time. Telemetry and energy remain unknown.

## Reproduce

The [verifier](../examples/verify_cuda_fallback.py) uses a private config with
`lab_only`, `namespace`, `base_manifest` path to a qualified suspended Job,
`database_url`, and a fresh bounded `run_ref`. Site images/host bindings/credentials
stay private. Require an idle qualified Kubernetes queue, Ready/no-pressure node,
and existing read-only source/runtime bindings. The source benchmark must match
the repository's actual code before submitting.

```sh
uv run python examples/verify_cuda_fallback.py \
  --config /secure/cuda-fallback-config.json \
  --directory /secure/new-cuda-fallback-trial
```

The report directory must be new. A failure/timeout requires inspection of the
same recorded IDs; never automatically repeat either Job. This proves this one
CUDA fallback gate. Driver version validation, all dependency-byte attestation,
general model accuracy/conversion, physical Pi NPU and full platform acceptance
remain separate boundaries. Twenty samples do not validate tail latency.
