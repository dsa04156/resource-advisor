# Observation without changing researcher code

The cooperative `observe` execution path still requires an `ExecutionResult`.
This separate path observes an **already running program** without importing an
SDK into it, launching it again, changing its environment, attaching a debugger
or sending it signals. The collector's time limit stops collection, not the
research program. No compute submission or model conversion occurs.

## Collect and import

An authorized operator first verifies the program's project, native Job identity,
node, PID namespace and process PID. A private binding file contains only:

```json
{
  "ref": "observation-example-001",
  "project_ref": "research-team-a",
  "target": {
    "backend": "kubernetes",
    "backend_cluster_id": "research-lab",
    "node_ref": "gpu-node-example",
    "external_job_ref": "existing-research-job",
    "external_job_uid": "immutable-job-uid",
    "attempt_ref": "existing-attempt"
  }
}
```

These are examples, not a registered execution or hardware proof. For Slurm,
use `native_submitted_at` with the cluster and native Job ID, and omit the
Kubernetes UID. For a platform-owned ComputeJob, also supply `platform_job_id`;
the API checks project, attempt, backend/cluster, native identity and node against
persisted execution metadata. External execution/process association remains
operator-attested, not remotely attested by arbitrary JSON.

Run in an authorized Linux/Python 3.11+ environment with read access to the
existing target's `/proc` entries. No privileged host mount is installed by this
tool. Permission-dependent counters stay missing. The source never reads target
command arguments, environment, memory contents or model/checkpoint files.

```sh
python -m resource_advisor.passive_collector \
  --binding /private/observation-binding.json --pid 1234 \
  --duration 15 --interval 1 --output /private/observation-001.json

python examples/import_observation.py \
  --report /private/observation-001.json \
  --api-config /private/observation-api.json
```

The API config has `api_url`, `operator_token` and an optional `ca_file`; keep it
outside the public repository. HTTPS verification stays enabled. An exact import
retry returns the same immutable record; a changed record under the same reference
returns 409. Ordinary researchers can read their project's observations but
cannot import trusted telemetry. Another project sees neither records nor details.

For a verified physical NVIDIA device, `--gpu-uuid '<physical UUID>'` enables
read-only NVML sampling without initializing CUDA in the observer. Never choose a
device by assuming index zero belongs to the Job. Missing NVML or unsupported
sensors remain explicit. Jetson/NPU sensor collection is not claimed verified
through the NVIDIA discrete-GPU provider.

## What the measurements mean

- Process CPU counters and approximate RSS describe one process thread group,
  excluding child processes. `sampled_peak_rss` is the highest observed point,
  not an exact lifetime memory peak. PID/start time, boot and PID namespace are
  hashed to detect a changed target; reused PIDs cannot extend the same trace.
- Node counters describe the collector's `/proc` view. They are not a substitute
  for verified scheduler allocatable capacity or container resource limits.
- NVML utilization, memory, power and temperature describe the **physical GPU**,
  including other users. They are never relabeled as this process's utilization.
- Quality, training step time, throughput and process network bytes are `null`.
  No model hooks or packet attribution were used. The API rejects non-null claims
  in those fields rather than creating a false performance profile.
- Collection overhead is recorded as query duration. The bounded loop can finish
  after its last sensor query; it is not a hard real-time or zero-overhead guarantee.
- `WINDOW_COMPLETE` means the collection window ended. It says nothing about
  native execution success, cancellation, result correctness or allocated GPU time.

The [Linux proc documentation](https://docs.kernel.org/filesystems/proc.html)
defines the counters and approximate RSS semantics. The
[NVML reference](https://docs.nvidia.com/deploy/nvml-api/latest/index.html)
describes device queries; available functions depend on the actual driver/device.

## Storage and console

`POST /api/v1/compute/observations` is operator-only.
`GET /api/v1/compute/observations` and `GET .../observations/{ref}` are project-scoped.
Reports use a distinct immutable `passive_observation` entity in the existing
metadata database. No ComputeJob, outbox, usage ledger or recommendation profile
is created or modified by import. Existing cooperative execution remains intact.

The Jobs page contains a collapsed **코드 변경 없는 관측** section to preserve the
compact main execution board. Details show individual timestamps, process counters,
physical GPU readings and original provenance. These are recorded collection
windows, not fabricated live samples or an MLflow model-quality result.

## Acceptance status

Implementation and focused contract checks cover import ownership, exact replay,
immutable conflicts, identity matching, missing metrics and PID reuse. An actual
ordinary Python process survives collection with unchanged source and output.
This is local process evidence, not Kubernetes/Slurm GPU hardware acceptance.
The source-only API rollout now passes pinned ArgoCD sync and actual HTTPS
read/ordinary-researcher denial checks. Existing 4,476 immutable entities,
592 job identities, 591 terminal Jobs and 591 ledger records were unchanged;
static service specifications/UIDs, Secrets, PVCs and quota were preserved.
See [deployment evidence](../evidence/passive-observation-deployment-v1.json).
The exact source commit's CI passed Python 3.11/3.13, SQLite/PostgreSQL,
lint/format, wheel/assets and Ansible syntax checks.

The subsequent [native CUDA acceptance](passive-gpu-observation-results.md)
passed the [prospectively frozen one-Job plan](passive-gpu-observation-results.md):
12 process/node/NVML samples, unchanged ordinary program, collection ending
while the program remained Running, later normal termination, project read/403/404,
exact replay and unchanged existing metadata/settings. One actual Job consumed
41 scheduled-Pod reservation proxy seconds, including 5s preparation and 36s
container time; exact quota release timing remains bounded. Its owned Job/ConfigMap were removed and the queue
released. Physical-device UUID matched the program's CUDA UUID. This closes the
bounded native CUDA fixture, not Slurm/Jetson/NPU passive sensors, all researcher
programs, per-process GPU utilization or the full platform acceptance scope.
