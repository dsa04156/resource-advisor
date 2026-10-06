# Native CNN result contract acceptance

The isolated Jetson runtime has passed a fixed numerical CNN gate. Its Python
3.10 interpreter cannot import the platform package, which requires Python 3.11
or newer. Keep the control-plane dependency requirement and use the standalone
[`slurm_cnn_workload.py`](../examples/slurm_cnn_workload.py) producer beside the
qualified CNN source. The existing worker parses the v1 envelope, validates the
Pydantic contract, digest and job/attempt/epoch/workload/context identity. The
producer is not a replacement for those checks or an authorization mechanism.

The new command must preserve the fixed one-CPU/one-GPU/1,024-MiB, zero-swap
boundary, FP32 `[4,3,32,32]`, seed 20261006 and ten measured forwards. It rejects
changed resources, runtime, device, shape, seed, precision, work count, shared
allocation, sampling/thermal policies and pilot mode before importing PyTorch.
An exact native runtime manifest must pin both source files and all relevant
runtime inputs before enabling automatic submission.

One work unit and one throughput unit mean **one four-input forward**. The timed
boundary includes the forward and GPU synchronization, excludes setup, transfers
and reference checks, and is not job completion time. The quality value is full
numerical agreement across 400 comparisons, not classifier accuracy. Peak memory
means PyTorch tensor allocation; the separate qualification record retains RSS
and cgroup peaks. Unmeasured GPU utilization, power and temperature remain null.

Before live acceptance, commit the producer and tests. Submit at most one new
normal-QOS, two-minute Slurm allocation using an exclusive submission intent,
retaining both qualification and result records plus parent/step accounting.
Use explicit acceptance-only identities; do not present them as API-created jobs.
Require both the existing strict CNN gate and a round trip through the current
platform `ExecutionResult` validation/digest calculation. Preserve failed trials
and costs; observation timeouts require inspecting the same job, never replay.

This test does not enable an API route. The read-only observer identity remains
unchanged. Scoped executor credentials, full runtime manifest, API submission,
artifact/MLflow/accounting publication, cancel and cross-user tests remain open.
Workers currently claim a shared outbox; a Slurm-only worker must not be added
beside a Kubernetes-only worker until route ownership is handled, or either
worker could claim a job for a backend it does not implement.
