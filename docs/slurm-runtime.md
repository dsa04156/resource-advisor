# Slurm runtime and result boundary

A Slurm job must execute the environment actually qualified for its variant.
The earlier adapter quoted the workload command but did not execute its declared
container image. This could produce a successful host execution with a misleading
image identity. Container-qualified Slurm variants now fail local preflight;
a container executor is still an open implementation gate.

## Qualified native execution

A native `RuntimeVariant` has `image: null`. Its operator route must contain a
`native_runtimes` binding for that exact variant, matching `environment_digest`.
The binding pins the runtime guard and manifest by SHA-256 and lists the exact
permitted command arrays. All-zero example digests are placeholders and must
be replaced with actual qualification hashes.

The batch script verifies the installed guard's hash before invoking it through
`srun`. The standalone standard-library guard then checks:

- the manifest's byte hash and declared qualified environment;
- the host architecture and exact command array;
- hashes of the executable, source, model/data/configuration and dependency files
  listed by the operator;
- deterministic runtime probe stdout hashes, for example Python/driver versions.

Use absolute paths, install the files on the selected worker and keep them
operator-owned/read-only. Enumerate **all relevant runtime inputs**, including
libraries and models, when qualifying a real workload. The guard verifies the
listed inputs; it cannot infer that an incomplete manifest describes the whole
environment. It does not install packages or upgrade drivers. It does not
provide filesystem/device isolation or defend against a privileged writer racing
the checks. Those remain separate admission/qualification requirements.

Minimal manifest shape (example values, not qualified hardware evidence):

```json
{
  "schema_version": "v1",
  "kind": "native",
  "architecture": "aarch64",
  "environment_digest": "sha256:<qualified environment hash>",
  "commands": [["/opt/research/bin/python", "-I", "/opt/research/workload.py"]],
  "files": {
    "/opt/research/bin/python": "sha256:<actual executable hash>",
    "/opt/research/workload.py": "sha256:<actual source hash>"
  },
  "environment": {},
  "probes": [{
    "command": ["/opt/research/bin/python", "-I", "--version"],
    "stdout_digest": "sha256:<actual stdout hash including newline>"
  }]
}
```

The manifest byte hash is separate from the qualified environment identity.
The guard and batch step use `/` as their working directory. Ambient Python,
preload and credential variables are removed; required environment settings must
be declared in the hashed manifest. Scheduling/device visibility and the bounded
job identity variables are preserved. A manifest cannot replace them.

The worker performs pure local adapter validation before committing submission
intent. A missing binding, image executor or priority mapping becomes a terminal
`PRE_SUBMISSION_BACKEND` failure with zero allocated resource time. A network
failure after the `sbatch` call still remains `SUBMISSION_UNKNOWN`; it is not
relabelled as a safe-to-retry rejection.

## Node-local output transport

`result_ssh_targets` maps qualified Slurm node names to operator-configured SSH
aliases. The adapter first requires one accounting row matching job ID, attempt
name, project account, qualified node, COMPLETED state and exit 0:0. It then reads
at most 65,537 bytes from the configured output path on that allowlisted worker.
Strict host-key checking and noninteractive key authentication remain mandatory.
Without this map, the same accounting check precedes a controller/shared-storage
read. No hostname or SSH destination supplied by a job is executed.

Reconciliation also filters and verifies account plus attempt name, preventing a
same-name job in another account from being attached. This is not a claim of
cross-user device isolation. Scheduler/accounting outages remain errors and never
become evidence of absent work.

The [ownership update](slurm-ownership.md) extends this boundary to live status,
terminal accounting and cancellation: account/partition/attempt are checked,
ambiguous accounting IDs are rejected, and controller-side cancellation filters
limit the requested operation. Live multi-user verification remains open.

## Explicit CPU-only gateway scope

The adapter already omits GRES for a zero-accelerator request. The forced SSH
gateway formerly accepted only the fixed one-accelerator fixture, so it rejected
that CPU script before `sbatch`. An operator-owned gateway configuration can now
set `cpu_only: true` for a separate CPU route. Existing configurations default to
the original accelerator scope; a job cannot choose this setting.

The CPU scope fixes one CPU, 1024MiB host memory and zero accelerators, requires
`allocation_mode: cpu_only` and `memory_model: host`, and forbids a configured or
submitted GRES. The same fixed node/account/partition/QOS/output path, time bounds,
identity exports and exact qualified runtime tail remain required. This grants
neither automatic CUDA fallback nor permission to run arbitrary commands.

[`slurm-cpu-gateway.example.json`](../examples/slurm-cpu-gateway.example.json)
is an intentionally inactive configuration: its empty native bindings refuse
all submissions until an operator qualifies the CPU environment and fills the
exact guarded tail. Use a separate SSH identity/configuration for each qualified
CPU node; do not replace the existing GPU route. Package/driver installation and
root gateway deployment are not performed by the example.

The original CPU-script rejection was reproduced, then the CPU scope and resource,
GRES, allocation-mode, runtime and opt-in negatives passed alongside existing GPU
adapter/gateway checks (36 focused tests). These are software contract tests,
not actual Slurm CPU executions. The same six retained two-project identities
were rechecked read-only on October 7; controller observations remain unavailable,
with zero submit/cancel calls. Two-node CPU execution and Pi NPU qualification
remain incomplete. No live gateway, scheduler configuration or old Job was changed.

## Evidence and open gate

The controller and both workers [recovered on October 6](slurm-recovery.md).
A subsequent [real Orin PyTorch CNN attempt](slurm-torch-results.md) passed
numerical checks but remains blocked for automatic execution because of an
explicit runtime support warning and host-memory overrun. A separate
[Jetson runtime follow-up](slurm-jetson-runtime-results.md) now passes the fixed
CNN with native `sm_87` and enforced memory limits, retaining the failed candidate.
A subsequent [native CNN contract trial](slurm-cnn-contract-results.md) also
passes the platform's result model, canonical digest and identity checks, using
a standalone Python 3.10 producer. Its acceptance identities are not API jobs.
The subsequent [browser/API trial](slurm-api-results.md) now verifies scoped
execution credentials, a full native manifest and API/model-result/publication.
Full Slurm cancellation, multi-user and recovery gates remain open.
The earlier disconnected observations below are historical.

A real ARM worker ran the installed guard successfully, then rejected a changed
source before its command executed. See [the sanitized guard evidence](evidence/native-runtime-guard.json).
Unit tests cover manifest/environment/architecture/command/probe mismatches,
credential filtering, foreign-account reconciliation and result-node mismatches.

The controller became unreachable from both the API host and the ARM worker
during this increment. No successful live API → Slurm → model-result execution
is claimed. Earlier CUDA/accounting/QOS trials remain historical evidence;
restored controller access, a fully qualified model environment, actual worker
credential provisioning and end-to-end result/failure trials are still required.

Reference command fields: [squeue](https://slurm.schedmd.com/squeue.html) and
[sacct](https://slurm.schedmd.com/sacct.html).
