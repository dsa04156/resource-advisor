# Browser → API → Slurm GPU result

On October 6, a browser submission executed the qualified native CNN on a real
Orin GPU through the deployed API and Slurm worker. The successful result has
one PostgreSQL allocation row, one MLflow run and identical result-bundle bytes
read back from S3, the project API and MLflow. The console shows the completed
Slurm job, quality status and result/tracking details.
[Machine-readable evidence](../evidence/slurm-api-v1.json) retains both attempts.

| Attempt | Outcome | GPU reservation | Queue wait |
|---|---|---:|---:|
| First browser submission | Failed CUDA initialization; retained in MLflow/ledger | 74 s | 1 s |
| Submission after access correction | SUCCEEDED; validated hardware result | 72 s | 0 s at scheduler resolution |
| Total | Both attempts retained | 146 s | Whole-second precision |

The new executor initially lacked access to the qualified DRM render device.
Its controller and worker have different numeric `render` group IDs. A named-user
ACL grants only that executor access to the required worker device; an exact
device-path udev rule reapplies it on recreation. Existing owner/group/ACL entries,
GPU reservation and cgroup limits remain in place. This avoids granting an
unrelated controller group merely because its numeric ID matches. A reboot or
device-path change still requires rechecking access and qualification.

The successful generated CNN passed all 400 numerical comparisons. Ten measured
forwards took 11.013 ms in total, with a median of 1.076 ms and peak PyTorch tensor
allocation of 33.056 MiB. These are forward-only measurements, not end-to-end
latency. The console's result duration shows the total of the ten forwards.
GPU utilization, power and temperature were not measured and remain null.
One run does not establish a performance improvement or trained-model accuracy.

The 72-second reservation also pays for native runtime verification and startup.
The manifest covers 26,649 file paths, including resolved dependency and driver
files. Reading these files substantially increases startup time and charged file
cache: the cgroup peak was about 1,021 MiB against the unchanged 1,024 MiB limit.
This is a correctness baseline with little memory headroom, not an efficient
steady-state serving path. Actual model CPU time remains unknown.

## Use the deployed path

1. Open `/console` on your configured API origin and connect with a project token.
2. Open **가속기 호환성**, then find `slurm-orin-cnn-api-v1` / `orin`.
3. Inspect the resource request and current contract status, then select
   **관측 실행**. This demo requests one GPU, one CPU, 1 GiB, at most 120 seconds.
4. Follow the job in **실행 현황**; expand **실행 상세 · 결과 · MLflow** for evidence.
5. Inspect **대기 · 할당 이력** for scheduler reservation time, including failures.

The registered capability expires. A disabled button after expiration requires a
fresh operator check; old successful results do not establish current readiness.
An uncertain submission response retains its idempotency key for a retry of that
same request. Once a submission succeeds, another click deliberately creates a
new job and incurs another allocation.

## Execution boundary

`examples/slurm_executor.py` is a root-owned forced SSH command for a dedicated,
password-locked, non-sudo account. Controller mode accepts only the adapter's
bounded submit/status/accounting/cancel commands and forces Linux-owner filtering.
Its root-owned configuration pins account, partition, QOS, node, GRES, output
directory and exact native command tail. Result mode on the worker only reads
bounded, non-symlink, executor-owned output files. Arbitrary shell commands were
denied on both live SSH targets. The existing inventory account remains read-only.

`deploy/slurm-worker` starts at zero replicas until private route configuration
and credentials are supplied. The deployed worker claims only its project/cluster
route. The existing Kubernetes worker continues publishing the shared project's
MLflow/artifact outboxes but cannot claim its Slurm compute work. Study
coordination is disabled on the Slurm worker to avoid duplicate planners.
ArgoCD manages these services, while the API submits runtime jobs.

This gateway assumes a trusted control plane and root-owned runtime files. It is
not an arbitrary-program sandbox, a container executor or a complete multi-tenant
security boundary. Native guard semantics are described in [slurm-runtime.md](slurm-runtime.md).
The Jetson runtime pairing remains experimental rather than vendor-certified.

Validation was limited locally to the changed gateway/guard checks and six console
tests, plus the actual browser executions above. GitHub CI also passed for the
deployed source commit. Full Slurm cancellation, cross-user and recovery scenarios,
the physical Pi NPU, and the remaining [v0.3 gates](../right-sizing-claim-audit.md) remain open.
