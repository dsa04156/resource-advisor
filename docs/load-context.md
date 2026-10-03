# Container load context bound to execution results

The optional RuntimeVariant policy `linux-cgroup-v2-brackets-v1` requires an
execution-bound load trace before a completed result can enter the profile
history. Existing variants omit this field when serialized, retaining their
previous signatures. Enabling it changes the execution-context signature;
instrumented and historical uninstrumented timings are not silently pooled.

The reader only observes its own private cgroup-v2 namespace. It verifies the
mount type, process membership and stable cgroup identity. It never mounts a
host filesystem, changes a controller, writes a limit or scans another process's
command line. Unsupported namespaces fail explicitly.

## What is measured

Two readings bracket the cooperative runner, including its setup, reference,
warmup and output validation. This window contains the useful compute interval
but is not that interval. The optional pre-execution wait lies outside both
windows and still contributes to scheduler allocation.

| Observation | Meaning and limits |
| --- | --- |
| CPU usage delta | CPU time charged to the container and descendants, including co-runners |
| Throttled time and periods | CPU quota throttling counters, not host or GPU utilization |
| CPU/IO PSI `some` totals | Resource stall counters for this cgroup; unavailable files remain null |
| Block-I/O read/write bytes | Attributed block-device traffic, not application bytes, cache hits, network bytes or device saturation |
| Current memory | A sampled container gauge, not a peak and not GPU memory |
| Process count | Distinct visible processes at each read, not all concurrent jobs over the interval |
| CPU/memory limits | Must match the declared resources at both readings and remain unchanged |

An existing empty `io.stat` means no attributed block-I/O rows and yields zero
bytes. A missing or inaccessible file means unknown. Byte counters aggregated
across devices must not decrease. Reads span multiple kernel files and are not
atomic snapshots; their elapsed overhead is retained. Parent cgroup limits can
further restrict a container, so the observed leaf quota is not a claim of
available CPU capacity. See the kernel's [cgroup v2 interface](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)
and [PSI definitions](https://www.kernel.org/doc/html/latest/accounting/psi.html).

## Collection and persistence

`load_observed_benchmark.py` wraps the existing deterministic CUDA CNN fixture.
It checks explicit opt-in and actual CUDA availability, captures the two readings,
then emits exactly one result envelope containing `load_trace`. It withholds an
early printed result if the workload subsequently fails, emits duplicates, or
produces a mismatched identity/digest. It does not provide CPU inference fallback.

The collector requires job, attempt, context and result-digest equality. Reversed
windows, intervals shorter than useful compute, changing limits, counter resets,
disappearing counters and invalid numeric values are rejected. Invalid telemetry
creates no result/profile/artifact transaction; collection retries the same
attempt until the existing collection deadline. A terminal trace is immutable.
Other workloads cannot attach arbitrary load data without the qualified policy.

Validated traces are stored alongside the result in SQL and copied to the S3
result bundle. MLflow records the trace digest, scope, missing-counter names and
available summary metrics. `GET /api/v1/compute/jobs/{job_id}/load-context`
returns the trace and summary for the owning project; uninstrumented jobs return
`NOT_MEASURED`, and another project's token receives 404. The operator-controlled
collector/runtime remains the trust boundary; a schema is not hardware attestation.

This increment does **not** infer a slowdown's cause, attribute all CPU usage to
the model, count host-wide concurrent workloads or automatically change requests.
Thermal evidence remains its separate NVML trace. No uncertainty or drift
threshold is relaxed by adding load telemetry.

## Verification scope

Synthetic file/scheduler tests cover the reader, counter and identity faults,
legacy signature preservation, terminal immutability, project authorization,
SQL/artifact/MLflow propagation and output-withholding behavior. They are not
hardware benchmark evidence.

A read-only probe of the existing service container verified a private cgroup-v2
namespace, visible own PID and all eight required/optional interface files. The
actual new reader obtained two observations under its existing two-CPU quota;
the later read took approximately 0.366 ms. No stress or GPU work was submitted,
and this probe alone does not qualify the GPU runner's cgroup layout. The subsequent
[ten-Job CUDA acceptance](load-drift.md) now verifies the actual GPU container,
API/SQL/S3/MLflow trace propagation and stale-approval rejection under the
[prospectively fixed protocol](load-drift-plan.md).

```sh
uv run pytest -q tests/test_load_context.py tests/test_load_observed_benchmark.py
```

Before enabling a qualified workload, deploy compatible API and collector code,
qualify the exact source/runtime and container cgroup behavior, and freeze a new
RuntimeVariant and workload context. Never activate the policy on old immutable
variants or assume that a successful service-container probe qualifies Slurm,
EdgeCore or every container runtime.

The qualified lab API and worker now run source commit `5a7d5dc`; the
[rollout record](evidence/load-context-deployment.json) retains the image/source
digests and preservation checks. Existing uninstrumented jobs still return
`NOT_MEASURED`; no old variant was rewritten.
