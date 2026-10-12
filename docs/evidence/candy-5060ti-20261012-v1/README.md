# RTX5060Ti additional Candy cohort

See [the Korean comparison](../../candy-5060ti-20261012-v1-results.ko.md).
The native device is **NVIDIA GeForce RTX 5060 Ti**, not a measured non-Ti RTX5060.
This extends the previous Mobilint/RTX5080 observations with three fresh Job/session
replicates. It is a later, single-device cohort; the previous devices are not
concurrent randomized controls. Calls within one Job are nested, not independent
hardware replicates. All three Jobs run on the same physical RTX5060Ti.

Recompute the published data from the repository root:

```sh
python3 docs/evidence/candy-5060ti-20261012-v1/recompute.py
```

The script verifies all native public hashes, the immutable mounted ConfigMap,
unchanged probe/source hash, model/input hashes, exact native GPU identity,
resource requests/limits, image digest receipts, finite C-contiguous host outputs,
30 repeated-output hashes per Job, all 64 CUDA operator events in each audit trace,
CSV/summary and the digest of the previous reference summary.

`capture.json` stores all timed calls plus compressed output tensors and CUDA
placement traces. `native-records-public.tar.gz` contains each preparation/main
Job/Pod/log/manifest/receipt and the immutable code ConfigMap. The original/public
SHA256 correspondence is in `native-files-sha256.json`; original records remain
private. Supplier models/SDKs and private registry/node/IP values are excluded.

The executed `probe.py` is byte-identical to
[the prior protocol4 probe](../mobilint-5080-20261012-v1/probe.py), rather than a
new implementation. Use the [prior model preparation and package instructions](../mobilint-5080-20261012-v1/README.md)
and bind `gpu-template.json` placeholders to the private registry/device route
and existing queue admission. GPU identity must match RTX5060Ti. Keep batch1,
HWC224x344 float32 seed20261008, CPU1, memory request1Gi/limit4Gi, TF32-off and the
pinned artifact/image hashes. Qualification uses3samples/2warmups; each main Job
uses30samples/5warmups. Capture every outcome before interpreting it.

Both GPUs use the same prepared ONNX artifact and numerical input. The NPU MXQ's
original weights, precision and preprocessing equivalence remain unestablished,
and its prior measurement uses one core. Synthetic input[-1,1] does not establish
correct real-image RGB[0,255] semantics or inference accuracy. Device/node caches
were not cleared. The separate GPU placement audit is outside the timed loop and
inside native Job JCT. Loop throughput excludes setup/warmup/queue gaps. No
hardware-general improvement, accuracy, energy, significance or production
scheduling qualification is claimed.
