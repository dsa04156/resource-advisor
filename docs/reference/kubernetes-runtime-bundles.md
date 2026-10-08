# Explicit bindings for mounted Kubernetes runtimes

A pinned image is not necessarily a self-contained execution environment. Some
qualified lab images receive Python packages from a read-only runtime PVC and
runner code from a ConfigMap. Previously the worker selected those attachments
only by `RuntimeVariant.ref`; a new immutable variant reference silently lost
them even when its declared environment digest was unchanged.

An actual [server-outage trial](artifacts.md) exposed this: the new
variant entered Kueue and received a GPU, then failed before importing its runner
because the required package mount was absent. The original failed attempt and
its1GPU-second cost are retained. It is not a successful accelerator benchmark.

Mounted variants can now declare an opaque operator-owned reference:

```json
{
  "kubernetes_runtime_bundle_ref": "cuda-lab-runtime-v1"
}
```

The project's worker configuration provides `runtime_bundles` under that key.
It holds the approved environment digest, runtime PVC and source ConfigMap.
The researcher cannot supply arbitrary volume names, host paths or mount flags.
New immutable variants can reference the same approved bundle without duplicating
its private worker configuration under every variant ID.

An explicitly required bundle that is absent fails backend validation BEFORE
native submission; a digest mismatch also fails. Selected runtime/source mounts
remain read-only, with the existing token/privilege restrictions. The reference
requires a pinned container image and is not accepted for a Slurm candidate.
The native Slurm runtime binding remains independent.

The explicit reference contributes to the execution-context signature, so
changing it cannot silently reuse a previous approval/profile. Omitted/null
references retain the old serialized variant and context signatures, and legacy
variant-keyed configurations continue to work. Self-contained images such as
the qualified Hailo image can keep the reference absent.

This does not inspect arbitrary images, attest every PVC byte, automatically
refresh expired capabilities or certify a model. The operator must qualify the
actual complete environment and protect the bundle's contents. A new contract
cannot be sent to an older API/worker until those services support the field.
Focused tests verify read-only reuse, missing/mismatched preflight rejection,
legacy hashes, new-context scope and Slurm exclusion. Commit `d64226c` passed
CI on Python3.11/3.13 with SQLite/PostgreSQL. The API and trusted Kubernetes
worker are now deployed through their existing manually synchronized ArgoCD
Applications; the running contract/backend/policy source hashes match the image
build reports. Both Applications are Synced/Healthy with successful operations.
Inventory, Slurm worker, artifact service and existing deployment identities
were preserved. All4461 non-inventory entities,589 ledgers and589 terminal
Jobs retained their content hashes. The unresolved Slurm cancellation retains
its original job/attempt/native identity; its observer retry metadata advances.

## Bounded live qualification, frozen before submission

Use exactly one fresh API observe Job with a new immutable workload/variant and
idempotency key. Keep the already qualified CUDA matmul fixture unchanged:
FP32,256×256,seed0,20 measured iterations, one physical GPU, one CPU and2GiB.
The only execution attachment change is the explicit reference to the existing
operator-approved bundle; do not add a configuration entry for the new variant
ID. Check the current node is Ready without memory/disk/PID pressure, its GPU
resource is registered, the runtime PVC is Bound and source ConfigMap exists.
Record a fresh versioned capability instead of refreshing old records.

Require native completion, actual CUDA/numerical agreement1, read-only runtime
and source mounts, matching SQL/API/S3/MLflow result bytes, one ledger and one
FINISHED MLflow run. Repeating the same API key must return the original attempt
without new compute. Stop on an unexpected result and retain its original IDs
and all allocation cost. No replacement Job, MLflow outage, quota/driver change
or new performance comparison is part of this qualification. Preserve the
previous failed server-outage attempt and its1GPU-second cost.

## Live result

The protocol was frozen in `33dc4a6` before the single accepted Job.
[Sanitized evidence](../evidence/kubernetes-runtime-bundle-v1.json) records PASS:
the new variant used the existing explicit bundle without a variant-ID mapping,
one native Pod completed without restarts, runtime/source mounts were read-only,
and20 actual CUDA matmul measurements passed numerical agreement1. The same
result bytes matched SQL/API/S3/MLflow, with one ledger and one FINISHED run.
Reusing the API key returned the original job/attempt; exactly one new native
Job existed. Existing immutable entities,589 ledgers,589 terminal Jobs and
native Job specifications remained unchanged.

The new allocation cost is2GPU reservation seconds and2CPU core-seconds.
Together with the separately retained failed outage allocation, this repair
has3GPU reservation seconds; reservations are not measured utilization or
energy. A preparation-script datetime-serialization error occurred before
registration/submission; a readback assertion initially used incorrect mount
names. Corrected read-only reconciliation inspected the SAME successful Job,
without a replacement allocation. Neither error is hidden as GPU work.

This closes the explicit mounted-runtime qualification for this fixture.
It does not turn the previous server-outage trial into a pass, qualify arbitrary
images/bundles or complete the original HAIRP scope. Temporary DB/S3/MLflow
verification forwards were closed; the persistent HTTPS API was preserved.
