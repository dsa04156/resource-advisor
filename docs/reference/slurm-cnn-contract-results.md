# Native CNN result contract — October 6

The [fixed acceptance plan](slurm-cnn-contract-results.md) passes on one real Orin
Slurm allocation. [Evidence](../evidence/slurm-cnn-contract-v1.json) includes source
hashes, all ten samples, numerical checks, the emitted v1 envelope and accounting.
The operator submitted this acceptance job; its explicitly named acceptance
identities are **not API-created jobs**. No API records or MLflow runs were added,
and no automatic runtime binding was enabled.

The unchanged isolated Python 3.10/Jetson runtime executed the new standalone
producer. The current Python 3.13 platform parser and `ExecutionResult` model then
accepted its output; the canonical digest and all five result identity fields
matched the planned values. No platform dependency or system interpreter was
changed. The numerical fixture retains the same input and weight digests and
passes all 400 comparisons at rtol/atol 1e-4, with maximum absolute error
`1.4901161193847656e-08` and native `sm_87` support.

| Observation | Actual result |
|---|---|
| Slurm parent / batch / model step | COMPLETED, exit 0:0 |
| Submit / allocation start / end | 2026-10-06 04:00:00 / 04:00:01 / 04:00:07 UTC |
| GPU reservation / CPU reservation / queue wait | 6 seconds / 6 seconds / 1 second |
| Forward median | 1.423224 ms; ten samples in one job |
| Throughput | 749.837938 four-input forwards/second |
| PyTorch peak tensor allocation | 33.056152 MiB |
| Process peak RSS | 790.144531 MiB |
| Limiting cgroup peak | 499.125 MiB |
| Enforced memory / swap / CPU affinity | 1,024 MiB / zero / one CPU |
| GPU utilization / power / temperature | Not measured; null in result |

RSS, cgroup charging and tensor allocation cover different scopes and are not
interchangeable; this unified-memory device has no separately measured VRAM here.
The forward timing excludes initialization, transfers and reference checks, so
it is not end-to-end latency or scheduler wall time. One job cannot establish a
performance improvement. This generated model provides numerical correctness,
not trained classifier accuracy or vendor certification of the runtime pairing.

`sacct` still omits model-step MaxRSS and reports zero TotalCPU for that short
step. Retain actual model CPU accounting as unknown, not zero; the batch shell's
small nonzero CPU/RSS values do not measure the model. Protected worker/controller
configuration, boot and driver identities, QOS/associations and idle queue state
match before/after snapshots. No reboot or daemon restart was performed.

The full local suite passes **924 tests, with two PostgreSQL-only skips**; the
new producer tests independently round-trip through the platform contract and
reject changed identities, shapes, runtime, allocation and partial/nonfinite
evidence. These software tests do not substitute for the real job above.

Remaining Slurm gates include a complete native runtime manifest, scoped
execution credentials, worker route ownership, API→Slurm→artifact/MLflow/ledger
execution, cancellation and cross-user isolation. The restricted inventory
observer keeps its existing read-only permissions. The physically absent Pi NPU
remains unqualified.
