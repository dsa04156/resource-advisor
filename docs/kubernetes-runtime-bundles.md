# Explicit bindings for mounted Kubernetes runtimes

A pinned image is not necessarily a self-contained execution environment. Some
qualified lab images receive Python packages from a read-only runtime PVC and
runner code from a ConfigMap. Previously the worker selected those attachments
only by `RuntimeVariant.ref`; a new immutable variant reference silently lost
them even when its declared environment digest was unchanged.

An actual [server-outage trial](mlflow-server-outage.md) exposed this: the new
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
legacy hashes, new-context scope and Slurm exclusion. Live deployment and a
fresh complete-environment execution remain pending for this increment.
