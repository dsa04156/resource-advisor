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
This increment has **no new live GPU sampling results**. Existing published
GPU trials repeat identical inputs and remain labeled repetition-only.
Paired rank validation, thermal evidence, qualified GPU runner integration,
automatic MF-KG coordination and equal-budget comparisons remain open.

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
