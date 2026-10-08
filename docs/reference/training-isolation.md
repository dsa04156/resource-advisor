# Isolated checkpoint trials on Kubernetes GPUs

The first qualified training path now runs every pilot and confirmation from a
separate copy of the same immutable initial checkpoint and input. Trial state is
never automatically promoted into the original training. This is a bounded JSON
checkpoint implementation with a deterministic SGD fixture, not general training
support or distributed restart orchestration.

## Why a separate binding

`TrainingIsolation` is an operator-owned, project-scoped registry record, keyed by
workload reference. It binds the exact WorkloadSpec digest, initial checkpoint and
input digests/sizes, qualification references, backend and output policy. The
checkpoint digest must equal both `profiling.checkpoint_digest` and the training
identity's `model_digest`; `dataset_version` is the input file digest. Thus a new
checkpoint changes the logical workload identity and cannot borrow incompatible
profiles. Existing v1 workload/result schemas and historical digests are unchanged.

`POST /api/v1/compute/training-isolation` requires an operator credential.
Submitting a training job also requires `TRAINING_VERIFIED` GPU qualification and
the matching binding. Only host CPU/memory changes can be declared mutable on this
path. Batch, global batch, precision, optimizer and runtime parameter overrides are
not searched. Unsupported or missing bindings fail before scheduler submission.
Slurm training isolation is explicitly rejected until that backend is qualified.

This registry is an operator qualification boundary, not cryptographic hardware
attestation. Operators must inspect actual execution evidence before registration.
An arbitrary claim of `TRAINING_VERIFIED` is not itself hardware proof.

## File boundaries

The worker's private route maps the binding digest to a source ConfigMap or PVC
and two safe file keys. Workload callers cannot supply arbitrary filesystem paths.

1. A non-root init container mounts only the two selected original files read-only.
   It checks exact file sizes/content hashes, copies them into a new per-Pod
   `emptyDir`, verifies the copies and rechecks the sources. Mismatch, symlinks,
   existing sandbox directories or unsafe route paths fail closed.
2. The training container receives only the copies at `/ra-checkpoint` and
   `/ra-input`, both mounted read-only. The original source volume is absent.
   `/ra-output` is a separate writable subdirectory of that Pod's ephemeral volume;
   it is never a writable alias for either input directory. `/tmp` is separate.
3. Containers run as UID/GID 1000 with a read-only root filesystem, dropped
   capabilities, no privilege escalation and no service-account token. The
   fixture verifies that write access to both input copies fails and that the
   original source path is absent.
4. Completed training must supply a `TrainingReceipt` binding the exact result,
   unchanged input hashes and output checkpoint digest. Result, receipt, terminal
   state and delivery outboxes are committed atomically. A missing/invalid receipt
   cannot yield a successful training profile. Late replacement is rejected.

Source files are currently limited to 1 MiB each; retained output is canonical JSON
of at most 16 KiB. The entire stdout envelope also retains the backend's 64 KiB
limit. A source can be binary at the copy layer, but the supplied training fixture
only reads JSON tensor lists and does not execute pickle checkpoint content.
Large/binary model output transport remains a separate implementation gate.

The terminal output is embedded in the S3 result bundle and linked MLflow artifact.
`GET /api/v1/compute/jobs/{job_id}/training-receipt` exposes it only to the owning
project. MLflow tags record initial/output checkpoint digests and
`training.auto_promote=false`. There is no endpoint that promotes a pilot checkpoint.
Artifact delivery failure preserves the completed compute record and retries through
the existing outbox; the ephemeral filesystem alone is not the durable artifact.

## Real test and cost boundaries

The live environment was Kubernetes **v1.31.14**, RTX 5080, PyTorch 2.8.0+cu128 and
CUDA 12.8. No Kubernetes/KubeEdge upgrade or existing runtime change was needed.
The same Kueue queue admitted every application attempt.

The deterministic fixture restores a linear model's weights/bias, **nonzero SGD
momentum buffers**, learning rate and step state from the checkpoint. It performs
ten FP32 steps with batch/global batch 8 and no accumulation changes. GPU warmup
uses separate restored state; the measured run then restores the initial state
again. GPU final weights and momentum are compared with a CPU reference outside
the timing boundary. Numerical agreement is not convergence or classifier accuracy.

- One initial GPU qualification passed; the actual Pod enforced the intended mounts.
- One pilot plus three independent confirmations completed through the API, study
  controller, Kueue, GPU worker and result validation.
- All four started from the same checkpoint and produced the same final checkpoint
  digest. The original ConfigMap bytes were unchanged after every trial sequence.
- Four result/checkpoint bundles were read back through S3, authenticated API and
  MLflow. MLflow tags and actual checkpoint contents matched.
- A separate negative source with one added byte failed the init digest check.
  Its Job failed and the training container never started. The genuine source was
  preserved. This failure is retained in the report.

Three confirmation runs averaged **4.050 ms for ten synchronized training steps**.
This excludes imports, preparation, model/optimizer setup, warmup, CPU reference
and artifact transfer. The four application ledger rows totaled **12 physical
GPU-seconds**; the successful qualification and negative preparation Job are outside
those rows. No speedup, complete experiment cost, full-training memory safety or
net resource saving is claimed.

The live test used an immutable ConfigMap source. PVC source mapping is implemented
but has not been qualified live. An independently mutable PVC needs an operator's
consistent source snapshot; mounting read-only does not freeze other writers.

## Reproduction

Generate the exact public fixture without using a private dataset:

```sh
uv run python examples/training_fixture.py /private/new-training-fixture
```

The command refuses an existing directory and emits hashes/sizes. Create an own
namespace immutable ConfigMap from `checkpoint.json` and `input.json`, or qualify
a snapshot-backed source PVC. Never point a writer at an existing training output.
Package `contracts.py`, `training.py`, `gpu_training.py` and `__init__.py` with the
qualified pinned PyTorch image/runtime; the entrypoint is:

```sh
python -m resource_advisor.gpu_training
```

Use `linear-sgd-ten-step-synchronized-v1`, shape `[8,2]`, FP32, 10 work units, batch
and global batch 8 in the workload identity. Include the actual source/environment
digests and generated checkpoint/input digests. Qualify real GPU execution and
numerical agreement before registering the variant as `TRAINING_VERIFIED` and
posting the operator binding. CPU reference execution alone does not qualify it.

The private Kubernetes route adds a `training_sources` entry to the existing
qualified runtime route:

```json
{
  "your-workload-ref": {
    "binding_digest": "sha256:<actual-TrainingIsolation-signature>",
    "source": {"config_map": "your-immutable-training-input"},
    "checkpoint_key": "checkpoint.json",
    "input_key": "input.json"
  }
}
```

For a PVC use `{"pvc":"your-qualified-snapshot"}` instead. The binding digest is
the canonical `signature(binding)` from the contracts module. Register consented
budgets before creating the study, use an idempotency key, save the returned study
ID and observe that same study. Test a separate corrupted source and retain its
failure; never corrupt the original. Inspect receipts, actual Pod mount/status
evidence and unchanged originals before calling protection verified.

[Raw JSON](../evidence/training-isolation.json) includes the reproducible input state,
all four results/checkpoints, qualification, negative test, per-attempt Pod checks,
accounting and artifact read-back. [CSV](../evidence/training-isolation.csv) gives one
row per application attempt. Stochastic RNG/data-loader state, larger checkpoints,
arbitrary training frameworks, Slurm isolation, distributed training, automatic
resume/promotion and interrupted-output retention remain open.

References: [Kubernetes volume subpaths](https://kubernetes.io/docs/concepts/storage/volumes/#using-subpath)
and [PyTorch checkpoint state](https://docs.pytorch.org/tutorials/beginner/saving_loading_models.html).
Actual compatibility evidence is the recorded v1.31.14 run, not an assumption that
all features from current documentation exist on every older cluster.
