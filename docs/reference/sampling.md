# Approved input sampling and execution receipts

Representative sampling now has an executable contract. An operator registers
a finite input manifest; each workload binds that policy into its identity; a
cooperative runner hashes and decodes the bytes it passes to its operation.
The trusted result collector requires an ordered consumption receipt before
accepting a successful sampled run. This is a prerequisite for R2, not approval
to use multi-fidelity optimization or discard slow configurations.

## What the initial policy covers

`SamplingPolicy` supports 4–128 distinct tensor JSON batches in 2–16 declared
strata. A sample contains its stable reference, stratum and SHA-256 digest of
the exact input file bytes. Duplicate references and duplicate content digests
are rejected. The policy fixes dataset version, input shape, precision (`fp32`
or `fp16`), batch size, seed, warmup count and an explicit maximum absolute
stratum-proportion error (default 0.05).

For a budget of `n` measured batches, the selector allocates stratum quotas by
largest remainder, breaks ties deterministically using the seed, and chooses
distinct members by a seeded hash ordering. Every declared stratum must occur;
a budget that cannot meet coverage or proportion tolerance is rejected. For
example, a ten-batch population split 6:4 yields a five-batch selection of 3:2.
Two batches would yield 1:1 and exceed the default tolerance.

Every lower-level selection is a subset of the full finite population. Different
lower-level selections are **not guaranteed to be nested prefixes**: largest
remainder can change a stratum quota non-monotonically. Comparisons must use the
saved sample list, not infer inputs from work units. The same first hash-ranked
input is used for warmup at every admissible budget; warmup never contributes
to measured sample count. Target fidelity must consume the whole registered
population. Training, arbitrary streaming datasets and compressed image formats
are outside this initial contract.

## Registration and immutable identity

All endpoints below use `/api/v1/compute` and project-scoped authentication.

1. An operator posts the typed manifest to `POST /sampling-policies`.
2. Set `WorkloadIdentity.sampling_policy_digest = signature(policy)` using the
   package's canonical `signature` function. Register matching runtime variants
   and short/target workloads. Only work units differ between fidelity levels.
3. An operator posts `{"workload_ref":"example-short","policy_ref":"example-inputs"}`
   to `POST /sampling-bindings` for each workload.
4. Register the representative fidelity space with that same policy digest.
   Ordinary jobs and calibration studies keep their existing submission paths.

Sampled workloads cannot execute before binding. A binding rechecks the policy,
project, identity and budget and is immutable. Submissions snapshot both the
full policy and derived plan. A policy cannot be attached retroactively to an
old workload identity: this prevents mixing unverified historical performance
with sampled runs. Identities with no sampling digest retain their original
serialized representation and signature, including existing database records.

Project members can inspect `GET /sampling-policies/{policy_ref}`. They cannot
approve or replace policies/bindings. Other projects cannot read them. Policy
manifests contain references and hashes, not the dataset or credentials.

## Runner integration

Both backend adapters supply `RA_SAMPLING_PLAN_JSON`. A runner integrates the
following sequence around its already-qualified accelerator operation:

```python
import json
import os
from pathlib import Path
from resource_advisor.sampling import SamplingSession, directory_reader
from resource_advisor.contracts import signature

session = SamplingSession(json.loads(os.environ["RA_SAMPLING_PLAN_JSON"]))
read = directory_reader(Path("/approved-inputs"))
session.warmup(read, qualified_operation)
for _ in range(session.plan.work_units):
    measurement = session.measure_next(read, qualified_operation)
    measurements.append(measurement)

# Build the normal ExecutionResult using the actual collected measurements.
# work_units and sample_count must equal the plan's measured batch count.
receipt = session.receipt(result)
envelope = {
    "result": result.model_dump(mode="json"),
    "digest": signature(result),
    "sampling_receipt": receipt.model_dump(mode="json"),
}
print("RESOURCE_ADVISOR_RESULT " + json.dumps(envelope))
```

`qualified_operation`, result construction and timing boundaries remain the
responsibility of the separately qualified runtime. GPU operations must finish
(including the required synchronization) before returning. This fragment is an
integration pattern, not a stand-alone GPU benchmark or qualification claim.
Hashing, JSON decoding and staging must not silently enter an existing
forward-only timing interval; keep that interval explicit and unchanged across
levels. Job wall cost still includes those activities when done inside the Job.

Each regular file `<sample-ref>.json` contains `shape`, `precision` and finite
numeric `values`; shape product must equal value count. The reader checks
SHA-256 before decoding, rejects changed shape/precision, over-range fp16/fp32
values and inputs over 1 MiB. Symlinks and nonregular files, including FIFOs,
are rejected without waiting for another process. Use a qualified image or
read-only dataset volume with regular files; projected ConfigMap symlinks are
not accepted. A failed read/operation poisons the session. Partial consumption,
duplicate warmup or exhausted measurement budgets cannot produce a receipt.

## Collection, provenance and limits

A receipt binds job/attempt/result digest, policy digest, selection digest,
warmup count and the exact ordered measured references/content digests.
Missing, reordered, duplicated or stale receipts cannot create accepted results
or performance profiles. Collection may retry until the existing collection
deadline, then terminates as `RESULT_INVALID`. Terminal receipts are immutable.
`GET /jobs/{job_id}/sampling-receipt` returns the project-owned accepted receipt.

Result bundles include the receipt, full policy and binding for reproducibility.
MLflow records receipt, policy and selection digests. Representative-fidelity
analysis rechecks the stored receipt against the immutable space and result;
missing/corrupted evidence is rejected. `sampling_policy_verified` and
`sampling_receipts_verified` mean this declared finite policy was applied with
valid receipts. They do **not** prove that operator-defined strata capture the
entire underlying data distribution or that the process is remotely attested.
A trusted, qualified cooperative runner is still required.

The bounded plan fits the environment transport; a 128-entry receipt fits the
existing 64 KiB result-envelope limit. These limits are deliberately small for
the initial input format. No result transport or backend admission limit is
silently enlarged.

`tests/test_sampling.py` exercises actual local file consumption and fault
injection, identity compatibility, permissions, collector acceptance, MLflow
tags and artifact contents using labeled scheduler/storage/HTTP doubles.
`tests/test_fidelity_space.py` rejects missing/altered receipts in analysis.
The initial contract tests alone were not GPU evidence. The subsequent live
trial below verifies one qualified GPU runner and finite generated population.
Earlier published GPU trials still remain labeled repetition-only.
Paired rank qualification, thermal evidence, automatic MF-KG coordination and
equal-budget comparisons remain open.

## Concrete CUDA runner

`examples/sampled_fixture.py --project example-project --ref example-inputs
--output /tmp/new-sampled-inputs` creates eight different 64×64 fp32 tensor files
and their finite-population policy. Four have negative values and four positive
values. This is an explicitly generated functional fixture, not a representative
real research dataset. A four-input lower level samples two files per stratum;
the eight-input target consumes all files. Each file represents one matrix.

`python -m resource_advisor.sampled_gpu_benchmark --inputs /approved-inputs`
consumes the backend-provided plan and computes `A @ A.T` on exactly one physical
CUDA device. It refuses CPU fallback, incompatible hardware/runtime/identity,
and shared-device contexts. Every measured input is checked against an fp64 CPU
reference; the result quality value is the worst per-input fraction of numerically
matching outputs. This is numerical agreement, not model accuracy.

The runner copies only approved bounded input bytes into a fresh private temporary
directory, validating each hash. This staging step deliberately supports projected
ConfigMap symlinks from an operator-owned source volume. It never writes back to
the original files. The sampling session then checks the regular copies again.
CPU reference computation, input creation/copy and result checking are outside
the timed CUDA-forward-plus-synchronization interval; Job wall cost includes them.
`RA_SAMPLED_METRICS` logs per-input times and numerical agreement separately from
the collector's aggregate result and receipt. No power/thermal qualification is
implied. Slurm's native runtime guard preserves the sampling-plan environment
variable; the new runner still needs independent qualification on each backend.

## Live verification — 2026-10-02 UTC

[Raw JSON](../evidence/sampled-gpu-calibration.json),
[CSV](../evidence/sampled-gpu-calibration.csv) and
[offline MF-GP/MF-KG analysis](../evidence/sampled-mfkg-analysis.json) retain the
actual results. Application source was `77d294ca25a1208e1ef50f1252148d5f01fd929d`.
The API, inventory and worker source-tree hashes were checked inside their
running Pods against the image build reports. Existing dependencies, CUDA,
Kubernetes and KubeEdge were retained.

The trial used Python 3.11.15, PyTorch 2.8.0+cu128 and CUDA 12.8 on one RTX 5080
with exclusive GPU allocation, CPU requests of 1 or
2, and the eight-file 64×64 fp32 fixture. Lower fidelity selected four distinct
inputs (two per stratum); target fidelity consumed all eight. Shape, precision,
batch, seed, operation, input policy and timing boundary remained unchanged.
Three seeded randomized complete blocks produced 12 independent probe Jobs,
then six new full-population confirmation Jobs. All 18 succeeded, with numerical
agreement 1.0 for every measured input. Inner input samples are subsamples, not
independent experiment replications.

| Mode | Inputs per Job | CPU | Independent Jobs | Mean timed forward total (ms) |
|---|---:|---:|---:|---:|
| Probe | 4 | 1 | 3 | 0.079507 |
| Probe | 4 | 2 | 3 | 0.079436 |
| Probe | 8 | 1 | 3 | 0.152701 |
| Probe | 8 | 2 | 3 | 0.151312 |
| New confirmation | 8 | 1 | 3 | 0.148809 |
| New confirmation | 8 | 2 | 3 | 0.147856 |

These tiny forward-plus-synchronization intervals exclude input preparation,
CPU reference calculation and transfers. They are not total Job duration or
GPU kernel busy time. The decision was `PRESERVE_BASELINE_UNCERTAINTY`: retain
CPU 1, because the independent confirmation does not establish a reliable
improvement. A lower arithmetic mean is not treated as proven superiority.

Every Job's Kueue Workload was joined by owner UID and showed quota reservation,
admission and completion. Every successful Pod requested/limited one GPU and
the expected CPU allocation. Only the six confirmations entered measured
recommendation history. All 18 receipts were validated from PostgreSQL and read
back through the authenticated API. Policy/binding/receipt/result bundles matched
byte-for-byte across S3, API and MLflow, with one FINISHED MLflow run per attempt.
Per-input log times summed exactly to each result's timed interval.

The study used **403.536461 seconds wall time and 38 GPU reservation seconds**.
Four separate F0 qualification Jobs cost another 8 reservation seconds. A fifth,
deliberately corrupted-input qualification Job failed before measurement with
the expected digest error and no result envelope; it cost 2 reservation seconds.
It used the same queue during the study, without concurrent GPU measurement,
and is not a controlled queue-contention comparison. The immutable original
input ConfigMap remained byte-identical. Final queues/outboxes were empty; all
ten cluster nodes were Ready with no pressure signal.

The analysis used only the 12 probes, rejecting confirmation leakage. Actual
`SingleTaskMultiFidelityGP` and `qMultiFidelityKnowledgeGradient` suggested another
CPU-1/fidelity-0.5 measurement, with `measured=false` and
`execution_authorized=false`. Local numerical planning took about 0.207 seconds
and is recorded separately from historical study costs. Database Job count did
not change. A live `strategy=mfkg` request still returned HTTP 422:
`MF_KG_DISABLED: no qualified paired-fidelity group`.

This proves the finite input-consumption and execution/provenance path. Dense
Gram matrices with different values do not establish a useful fidelity axis for
real model workloads. The declared two-stratum population is not a real dataset
distribution; thermal/load evidence and ranking stability remain unqualified.
No strategy, energy, utilization or cost superiority is claimed. Independent
Slurm and broader runtime qualification and the full v0.3 completion audit remain
open. A later [Hailo ResNet-50 contract](hailo-resnet50.md) independently passed
qualification; it does not establish a fidelity axis for this sampling trial. The
qualified package manifest and pinned image are recorded; this runner did not
capture a host-driver version, so its report does not establish driver parity.
