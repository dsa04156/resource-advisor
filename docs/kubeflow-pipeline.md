# Kubeflow launches; the compute backend owns the GPU

`examples/pipeline.py` contains a CPU-only, uncached launcher. It submits a
registered workload/candidate through the authenticated Compute API, polls the
job and fails the pipeline if the backend result is unsuccessful. A stable
`run_key` survives workflow retries so an uncertain HTTP response does not
create another GPU job. The worker, rather than the launcher, submits the
suspended Kubernetes Job to Kueue and validates the collected result.

## Build the launcher

Use an authorized registry and a **pinned Python 3.11 linux/amd64** base image.
The rootless build helper needs uv and [crane](https://github.com/google/go-containerregistry)
on PATH. It exports the existing lock, verifies wheel hashes, packages the
independent source, and runs as UID/GID 10001. No Kubernetes or API credentials
are included. It does not need a Docker socket or privileged build Pod.

```sh
python examples/build_launcher.py \
  --base '<qualified Python 3.11 image>@sha256:<digest>' \
  --destination '<authorized registry>/resource-advisor/launcher:<version>' \
  --workdir /scratch/new-launcher-build
uv run --extra pipelines python examples/pipeline.py --output /scratch/observe.yaml
```

Keep the generated build report outside the public repository if it contains a
private registry address. Qualify the resulting image with an actual Pod before
using its digest. The example currently selects amd64; it does not claim an
ARM launcher image. The measured lab build reuses an already qualified MLflow
base, which is larger than a dedicated minimal launcher image needs to be.

## Configure HTTPS and project credentials

For direct serving, `resource-advisor serve` accepts `--ssl-certfile` and
`--ssl-keyfile` together. An ingress terminating TLS is also compatible.
Keep a project-scoped API token in the pipeline namespace Secret; that Secret
must contain `token` and `ca.crt`. The latter is the CA bundle trusted for the
API endpoint. The pipeline mounts it and sets `SSL_CERT_FILE`; certificate and
hostname verification remain enabled. Never put token values in pipeline
parameters, run metadata, notebook cells or the image.

Pipeline parameters are `api_url`, `launcher_image` (digest pinned), `workload`,
`candidate`, `token_secret` (name only), and `run_key`. The workload and runtime
variant must already be registered, qualified and not stale. Start the worker
with a restricted Kubernetes route before submitting. API and database
processes must remain reachable for the entire workflow; a launcher success
alone does not prove artifact delivery or MLflow delivery.

## Acceptance evidence

Record the KFP run state, actual launcher resources, backend job/attempt,
Kueue admission conditions, GPU Pod exit status and device request, validated
result/quality, terminal usage row and final queue release. Preserve unsuccessful
attempts separately. A Pod eviction before API submission is an infrastructure
failure; it is not a GPU benchmark or a successful execution.

The existing Kubeflow storage repair is documented separately in
[kubeflow-recovery.md](kubeflow-recovery.md). Service readiness, pipeline
compilation, and complete live execution are separate acceptance gates.

## Live verification — 2026-10-02

The [sanitized evidence](evidence/kubeflow-pipeline.json) records a successful
uncached KFP 2.5.0 workflow through HTTPS, the independent API, Kueue and an actual
RTX 5080 PyTorch job. The launcher requested 100m CPU/128Mi and **no GPU**;
the backend workload reserved one physical GPU, one CPU and 2Gi memory.
The validated result had numerical agreement 1.0. The usage ledger recorded
2 GPU reservation seconds; the much shorter synchronized kernel measurement is
kept separately. Queue time is unknown in this trial because timestamps with
different precision overlap, rather than being reported as zero.

The result bundle was uploaded to the separate Resource Advisor S3 service,
read back byte-for-byte through S3 and the authenticated API, and linked to a
FINISHED MLflow run with its artifact. Final Kueue pending/admitted/reserving
counts were all zero. The workload was a 256×256 fp32 matrix multiplication
with 20 timed repetitions: this verifies execution wiring, not general model
performance, saturation, or optimization gains.

The first KFP attempt was evicted for ephemeral-storage pressure **before** an
API job existed. That failure was retained. Disposable Go cache cleanup restored
headroom; Kubernetes cleared DiskPressure naturally. The subsequent run succeeded
without changing eviction thresholds, scheduler protections or cluster versions.

A second successful **uncached** workflow reused the same `run_key`. Its launcher
returned the same job ID, attempt ID and result digest. The database still held
exactly one compute job for this workload, demonstrating workflow retry without
another GPU execution. This does not replace process-crash or network-partition
acceptance tests.
