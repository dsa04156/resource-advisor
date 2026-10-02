# Real GPU recommendation → approval → independent result

The previously missing final demo stage now has actual hardware evidence:
an unfamiliar qualified workload first returned `NEEDS_PROFILE`; three independent
GPU observations produced a measured recommendation; an immutable approval led to
a new GPU execution; the console compared the new result with its supporting runs.
All application jobs passed through the existing Kueue queue and Kubernetes worker.

## Environment and boundaries

One physical NVIDIA GeForce RTX 5080, PyTorch 2.8.0+cu128 and CUDA 12.8. The
runtime was requalified using a new immutable source ConfigMap and a separately
queued GPU correctness job. The new source/environment digest was registered
as a new capability/runtime/workload combination; an old capability's TTL was
not extended. The image remained digest-pinned. Existing Kubernetes, KubeEdge,
queue policy and node pressure protections were unchanged.

The workload is the same bounded CUDA matmul algorithm: two 256×256 FP32 matrices,
seed 0, TF32 disabled, three warmups and 20 synchronized timed operations. One CPU,
2 GiB host memory and one GPU were requested. The numerical reference runs outside
the timed GPU boundary. Numerical agreement of 1.0 means every tested output
passed the numerical tolerance; it is not an image-classification accuracy claim.

## Observed flow

1. No comparable profile in the freshly qualified environment: `NEEDS_PROFILE`.
2. Missing approval reference attached to a baseline request: HTTP 404, no job.
3. Three separately submitted observations completed and passed the quality gate.
4. Recommendation used exactly those three attempt IDs, then was explicitly approved.
5. A new fixed-mode GPU job used that approval and completed independently.
6. Identical submission replay returned the same job ID. A changed request using
   that key returned HTTP 409. Four application jobs existed at this point.
7. Three supporting runs and the new execution appeared separately in the evidence
   endpoint. S3/API result bytes, MLflow metrics and MLflow artifact bytes matched.

The supporting mean was **271.747 μs**; the first independent execution was
**275.806 μs**, a signed difference of **+4.059 μs**. This is a functional smoke
test with one approved configuration, not proof of a speedup or an optimal choice.
The console now chooses s/ms/μs/ns units so a small nonzero error is not rounded
into a displayed zero. Twenty inner samples do not establish reliable tail-latency
statistics or broad platform performance.

## Defects fixed from this trial

A supplied baseline approval reference previously escaped validation because
baseline execution itself requires no approval. The API now checks *every
supplied* reference for project ownership, candidate, workload digest and expiry.
Study probes use their reserved plan and cannot also claim a recommendation
approval. Ordinary baseline execution without an approval remains permitted.

Initial queue intervals were unknown because a client-observed submit response
could occur after the scheduler's whole-second PodScheduled timestamp. The
Kubernetes adapter now supplies the API server's Job creation timestamp as the
submission boundary. Queue time therefore includes admission and node-scheduling
wait, not container image preparation. It is not a separate Kueue-only duration.

A fifth application GPU job, under the same valid approval, verified this fix:
its ledger recorded 0 seconds from whole-second timestamps and 2 allocated
physical-device-seconds. The explicit `QUEUE_WHOLE_SECOND_RESOLUTION` flag means
this is **not proof of exactly zero waiting**. The initial four unknown queue
records remain unchanged. Each of the five application attempts recorded 2 device
seconds; the separate runtime qualification job is not included in that ledger.
Five result bundles/runs were verified through S3, authenticated API and MLflow.

## Evidence and reproduction

- [Raw public-safe JSON](evidence/approved-gpu-execution.json): runtime/workload
  identity, quality policy, qualification result, recommendations, approval,
  independent measurements, queue admission conditions, ledger and artifact checks.
- [Measurement CSV](evidence/approved-gpu-execution.csv): three observations and
  two later approved runs, with original measurement precision.
- [API verifier](../examples/verify_approved_execution.py): actual API calls and
  stable job keys, with accepted IDs saved before polling. Its raw output is private.

Register a fresh GPU capability/runtime and workload with no comparable history,
minimum three repeats and one qualified baseline candidate. Configure the own
namespace worker/queue and run it separately. Export a project token into `RA_TOKEN`
without placing it in source or shell command history. Then, from this repository:

```sh
uv run python examples/verify_approved_execution.py \
  --api-url https://advisor.example.invalid \
  --workload your-qualified-workload --baseline your-baseline \
  --run-id unique-demo-run --output /private/approval-demo.json
```

Use `--ca-file` with your trusted lab CA/certificate when needed; certificate
verification is never disabled. The verifier requires an unused output path and
stops on missing evidence, quality failure or a five-minute observation deadline.
An observation deadline does not prove a backend job stopped: inspect saved IDs
and its actual state before resuming work. Artifact delivery uses the separately
configured retryable worker outboxes; it is not implied by the API verifier alone.

This completes one Kubernetes/GPU demo path. NPU inference qualification, the
full Slurm model/API path, complete job-attributed utilization/power, measured
preparation/transfer costs and controlled policy comparisons remain separate gates.
