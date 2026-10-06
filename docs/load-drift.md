# Actual CUDA load-context and stale-approval acceptance

The [prospectively fixed protocol](load-drift-plan.md) completed on the qualified
RTX 5080: one qualification Job, three normal observations, three observations
with a CPU competitor inside the same container, and three normal recovery
observations. No replacement or additional tuning runs were submitted.

| Phase | Independent Jobs | Mean measured work time |
| --- | ---: | ---: |
| Normal baseline | 3 | 0.430207 s |
| Within-container CPU competitor | 3 | 0.789055 s |
| Normal recovery | 3 | 0.403198 s |

Each time is the sum of twelve blocks, each performing preprocessing, host-to-
device transfer and 32 deterministic CNN forwards with synchronization. It is
not single-request latency, pipeline latency or p95. Generated weights and inputs
test numerical agreement, which was 1.0 throughout; this is not trained accuracy.

The three competitor residuals were **+74.88%, +49.16%, +126.20%** relative to
the previously saved baseline. The unchanged rule—three consecutive deviations
in one direction exceeding 25%—invalidated the original recommendation after
the third competitor Job. A new lookup at that transition returned
`NEEDS_RECONFIRMATION`, without pooling the old and new groups into a usable
recommendation. Recovery residuals were −5.98%, −3.33% and −9.53%; the old
recommendation stayed invalid. One request using its old approval returned
**422 before Job creation**. No eleventh allocation was needed.

This is a fixed-order A→B→A functional acceptance, confounded with time. It is
not a randomized estimate of slowdown, a powered performance comparison,
shared-GPU interference or a calibrated probability of safe execution.

## Measured context and costs

All Jobs retained CPU 0.5, host memory 2 GiB and one physical GPU, under the
existing CPU 2/GPU 1 queue. CPU quota/usage/throttling, CPU/I/O PSI, block-I/O
bytes and process gauges were read from the actual container cgroup. Helper
start records and result traces name the same cgroup digest. Helpers ended with
their owned compute containers; no host-level stress was introduced.

The qualification collected a 1.0113-second inclusive cgroup window, with
1.545 ms spent on its two reads and 0.4198 seconds of useful work. Those boundaries
differ because setup, reference computation and warmup consume real resources.
An observed zero block-I/O counter means zero attributed traffic in that window;
it does not prove an idle storage device. Counter scope and missing-value rules
are described in [load-context.md](load-context.md).

Total reservation was **197 GPU-seconds**: 19 for qualification and 178 for nine
API Jobs. The whole protocol took **577.069 seconds**, including preparation,
registration, a worker configuration reload, collection and verification. Both
remained within the frozen 990 GPU-second and 1,800-second limits. Preparation
waits and collection costs were not removed to improve the apparent result.

Every API attempt produced one usage row, one MLflow run, matching API/SQL traces
and byte-identical S3/API/MLflow result bundles. Raw load and NVML traces remain
bound to the original result digests. All thermal and memory gates passed.

## Deployment, recovery and preservation

The API and worker received source-only images from verified commit `5a7d5dc`;
their running source hashes matched the build reports. Four existing manual-sync
Argo Applications reached Synced/Healthy. Dependencies, drivers, cluster versions,
inventory collectors, PostgreSQL and queue quotas were retained.

Predeployment helper failures are retained in the
[rollout record](evidence/load-context-deployment.json): incorrect parsing of a
registry port and a tuple/list snapshot comparison. Neither changed services.
The first post-rollout HTTPS read hit the replaced Pod; the existing systemd
tunnel automatically recovered. A local overlay reader was corrected to accept
its actual YAML format, and rendered API/worker image pins matched GitOps.

The first successful API Job's artifact readback encountered absent local S3 and
MLflow tunnels. Temporary read-only tunnels were restored and **the same completed
attempt** was read again. No replacement GPU Job was submitted. This was an
observer-side connection interruption, not a cluster delivery failure.

After the trial, original Job/usage/outbox/study rows and non-inventory entities
matched their pretrial content fingerprints after excluding the explicitly added
trial records. Static object specs/UIDs and unrelated running Pod identities were
preserved; only the worker runtime binding and its Pod reload were intentional.
PVCs and other Secrets were unchanged. Ten nodes remained Ready without pressure,
all four static applications were healthy and the queue/outbox were idle.

## Reproduce the audit

The [retained observation record](evidence/load-drift-v1.json) includes the ten
original result/load/thermal traces, context identity, competitor start evidence,
saved recommendation and validity checks, costs and preservation results.

```sh
uv run python examples/audit_load_drift.py docs/evidence/load-drift-v1.json
uv run pytest -q tests/test_load_drift_evidence.py
```

The auditor recomputes means, residuals, latching, ownership/digest consistency,
thermal eligibility and all ten allocation costs. Adversarial tests reject
missing/reused attempts, reset counters, foreign-cgroup helpers, altered results,
revived approvals, missing costs and chronology violations. These tests verify
the recorded evidence; they do not themselves execute a GPU or revisit storage.

Broader unseen-workload interval calibration, real storage/network contention,
Slurm model execution and other v0.3 gates remain open. This trial qualifies
this CUDA/cgroup/runtime path, not every device or scheduler.

A transient `RUNNING` → `QUEUED` observation during container exit led to a
separate [Job-completion handling fix](kubernetes-completion.md). Its regression
tests and worker rollout do not change this trial's results or add GPU runs.

The subsequent [S5 chronological offline replay](uncertainty-drift-ablation.md)
uses the same saved traces without new compute. It shows the three-result
trigger's delay and retained abstention after normal recovery; it does not
retroactively describe the trigger as preventing the earlier large errors.
