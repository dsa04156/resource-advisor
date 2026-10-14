# Dedicated ResNet50 inference workers

`render.py` creates two Deployment/Service pairs from private site values. The first
pair is CUDA on a qualified amd64 GPU node; the second is Hailo on a qualified arm64
Hailo8 node. Each image must be digest pinned and contain the qualified native SDK,
model artifacts, fixture, standalone runtime and model descriptor.

Provide `namespace` and exactly two `routes` with `name`, `runtime`, `image`, `node`.
Only CUDA library-search `runtime_env` entries (`PYTHONPATH`, `LD_LIBRARY_PATH`) are
accepted. Hostnames, image addresses and site values belong outside the public repo.

```sh
rtk proxy python deploy/inference-pool/render.py \
  --site /private/inference-site.json --output /private/inference-workers.json
rtk proxy kubectl create --dry-run=server -f /private/inference-workers.json
rtk proxy kubectl create -f /private/inference-workers.json
```

Inspect the actual device reservations before starting; two standing workers occupy
one GPU and one Hailo device. Recreate strategy avoids allocating a second accelerator
on image changes. Native startup qualification runs before readiness. A native error
invalidates READY, excludes the endpoint from routing and allows liveness restart.
No node reset, device/SDK upgrade, existing queue edit or production model release
is performed by this renderer.

The image contains `src/resource_advisor/inference_runtime.py` as
`/opt/inference/runtime.py`, a `/opt/inference/model.json` descriptor with `model_ref`,
`source_model_sha256`, `runtime`, `artifact_sha256`, `qualification_digest`, and
`/opt/fixture/{manifest.json,inputs.npy,reference.npy}` plus either `resnet50.onnx`
or `model.hef`. Qualification digest is `sha256:` plus manifest file SHA256. The
renderer supplies the final image digest as `RA_RUNTIME_IMAGE_DIGEST`. The two
qualified base images may contain distinct runtime dependencies; do not mix their
architecture/SDK environments or assume a HEF can be generated without its compiler.

Configure matching server-side endpoint bindings, release the API image through
its existing GitOps Application, and register a pool with an operator credential
only after both endpoints pass readiness. These lab worker resources are applied
explicitly from the private rendered manifest. Existing Argo service/worker apps do
not automatically own them; production GitOps adoption is a separate scope.

[API and operational boundaries](../../docs/reference/heterogeneous-inference.md).
