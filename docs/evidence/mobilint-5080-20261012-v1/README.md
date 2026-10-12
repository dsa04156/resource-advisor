# Reproduction and scope

See [the Korean report](../../mobilint-5080-20261012-v1-results.ko.md).
This is a synthetic deployment microbenchmark. MXQ/ONNX model equivalence, task
accuracy, energy efficiency and hardware-general speedup are unestablished.

Recompute the recorded results without accessing the devices:

```sh
python3 docs/evidence/mobilint-5080-20261012-v1/recompute.py
```

This checks the frozen schedule, source/model/input digests, all 180 positive finite
timings, C-contiguous output attestations, within-runtime output hashes, GPU
placement counts, native success/UIDs,
CSV values and JSON summary. The raw placement trace and one output tensor per
session are zlib/base64 fields in `capture.json`. Calls within a Job are nested;
the independent replication count is three Jobs per runtime.

`native-records-public.tar.gz` has final owned Job/Pod/log/manifest receipts and
the preserved FailedMount event. `native-files-sha256.json` maps original and
sanitized bytes. Private originals remain outside Git. `attempts.csv` includes
unsuccessful preparation attempts. The first failed CPU inspection Pod was gone
when final records were captured; its Job and previously captured event remain.

Prepare the public GPU model in an isolated Python environment with ONNX1.19.1,
ORT GPU1.23.2 and NumPy1.26.4. Run the following from this evidence directory:

```sh
python prepare_gpu_model.py /tmp/candy-reproduction
```

The script downloads an immutable ONNX repository commit, verifies the original
SHA256, adjusts static input/output shape, freezes initializer inputs, performs
basic constant folding and verifies the prepared SHA256. It checks all original
initializer bytes. CPU graph preparation is not a CPU benchmark result.

For a physical replay, use the existing licensed Mobilint runtime/model with the
exact protocol hash and qbruntime1.4.0. Use the pinned GPU package versions in
`gpu-packages.json`, the prepared model, and the existing compatible NVIDIA driver.
Supplier SDKs and model binaries are not distributed here. Native templates retain
the image digests but replace registry, IP and node with placeholders. Operators
must bind those placeholders to their private registry/queue/device routes.
Keep the protocol's resource requests/limits and exclusive device ownership;
do not bypass queue admission.

Mount the byte-identical `probe.py` into both native templates. Preparation Jobs
use `--samples 3 --warmups 2`. Each main Job uses `--samples 30 --warmups 5` and
the six-job order in `protocol.json`. Capture all native outcomes, including failed
ones, before interpreting results. The GPU wrapper verifies RTX5080 through the
CUDA Driver API. Timing requires host outputs; replacing it with an asynchronous
submission-only API would change the boundary.

`probe-v1.py` and protocol revisions 1/2 preserve the failed preparation history.
`probe-v2.py`, `protocol-v3.json`, `capture-v3.json`, `samples-v3.csv` and
`summary-v3.json` preserve the original six-Job pilot exactly. Its GPU copy used
NumPy's default K order after an NCHW-to-HWC transpose, so the HWC-shaped output
was not C-contiguous. Its 2.688ms mean excluded contiguous materialization despite
its boundary label. Those pilot timings do not satisfy the intended boundary and
are excluded from the primary result table. The old label is retained as executed
evidence of the deviation, not endorsed as accurate.

The primary run uses `probe.py` with protocol revision4. Both runtimes explicitly
copy into C order inside the timed closure and check C-contiguity afterward;
three fresh Jobs per runtime were remeasured with the same images/resources and
schedule. `recompute.py` also checks the archived pilot receipts and reproduces
its old CSV/mean/p95 without treating it as the corrected result.

Registration of a CPU provider is allowed, while CPU model-node placement is
rejected. Strict CPU fallback is disabled and a separate audited session checks
model-node placement; the audited session is outside the timed loop but inside
native Job JCT.

The published Candy model expects RGB[0,255]. Reusing the old NPU synthetic
[-1,1] fixture matches numerical input values, not validated image semantics.
Do not use this artifact as evidence of same-model inference accuracy or as a
Resource Advisor production qualification.
