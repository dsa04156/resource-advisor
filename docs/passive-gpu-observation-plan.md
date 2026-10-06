# Unmodified CUDA observation — frozen acceptance plan

**Prospective, not executed.** This is one bounded functional acceptance fixture
for design section 7.1, not a performance comparison or model qualification.
The [collector/API deployment](passive-observation.md) already has local process
and live service evidence. A local Python process cannot close the native GPU gate.

## Fixed fixture and limits

Use [the ordinary CUDA program](../examples/passive-gpu/program.py), unchanged
before, during and after collection. It imports PyTorch, not Resource Advisor;
no profiling hook or result contract is added. Native progress output is not a
validated performance result. Its physical CUDA UUID stays private; public
evidence uses a digest.

Freeze the program SHA-256 and private manifest inputs before submission:
qualified digest-pinned image, read-only qualified runtime PVC, allowed amd64 GPU
node, namespace/LocalQueue and operator/project identity. Never refresh expired
capability records to make this fixture look automatically qualified.

- At most **one native Kubernetes Job**, no retries or replacement trial.
- Existing Kueue queue/quota, one physical GPU, one CPU, 2 GiB; no quota edits.
- Program duration 35 seconds. Native active deadline at most 120 seconds.
- Queue observation deadline 120 seconds; cancel only this trial if not admitted.
- One read-only collection window: 12 seconds, 1-second interval, same Pod/PID.
- No driver, boot, runtime, HAMi, project policy or unrelated service changes.
- The observer stopping does not stop or restart the program.

Before admission, verify current node Ready/no pressure and the existing queue's
state. If unavailable, retain the outcome and stop; do not weaken protections.
The fixture uses an explicit hardware allowlist for qualification. Researcher
submission continues to use common SchedulingProfile routing.

## Required observations

1. Native Job UID, admitted Workload and actual GPU request identify the one Pod.
   Verify that PID 1 is its program, source bytes are unchanged and the operator
   binding names the same node/native Job/attempt. No command/environment/memory
   contents are read by the observer.
2. At least five stable process/node samples and five physical NVML GPU samples
   are retained. The observed physical UUID digest matches the actual CUDA UUID.
   Utilization and device memory are reported as device-inclusive values;
   unsupported power/temperature stay null with sensor errors.
3. Collection finishes while the original program is still Running. The original
   program then finishes with exit 0 and its normal output. Collection completion
   and native termination remain separate events.
4. Import the exact captured report with the project operator; read it through
   the researcher's API and console. Exact replay preserves the record digest.
   Ordinary-researcher import is 403; another project cannot read the record.
5. Quality, step time, throughput and process network remain null. No ComputeJob,
   measured model profile, MLflow execution run or allocation ledger is created
   by observation import. Existing immutable entities, attempts and ledgers remain
   unchanged except the single new observation record; inventory can advance.
6. Record actual Pod/container timestamps and reserved GPU seconds, including
   preparation/failure/cancellation. Do not equate query duration or kernel time
   with reservation cost. A missing interval stays unknown, never zero.
7. Verify live console missing-field wording and individual samples. Delete only
   the owned Job/ConfigMap after terminal evidence is retained; final queue release
   must be observed. Keep unsuccessful evidence and costs instead of resubmitting.

External process/native association is operator-attested, not remote attestation.
This fixture does not qualify Slurm, Jetson/NPU sensors, all researcher programs,
per-process GPU utilization, continuous monitoring or performance improvements.
Full M0–M6 and the broader initial HAIRP remain separate open acceptance scopes.
