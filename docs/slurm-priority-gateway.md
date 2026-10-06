# Common high-priority policy through the scoped Slurm gateway

The generic adapter already translated normal/high into QOS, but the deployed
dedicated gateway accepted only one QOS and its worker route had no high mapping.
This prevented an operational high-profile API path despite earlier direct Slurm
priority evidence. The gateway now accepts only its root-owned scope's explicit
normal/high map. Unconfigured grades/QOS, malformed maps, inconsistent defaults
and all previous account/owner/runtime/resource escapes remain rejected.

The existing lab user was already authorized for both QOS. No Slurm scheduler
configuration, association, quota, node software or native runtime changed.
The old gateway/scope were backed up; the deployed source hash matches the public
file. Only the idle independent worker's route mapping was added, followed by Pod
recreation. Private deployment inputs were updated so a later rollout preserves
the same mapping. Original normal-only profiles remain unchanged.

An immutable **Slurm high priority** profile now exists in the live project.
Selecting its registered native Orin workload submitted one actual GPU API job:

| Boundary | Direct observation |
|---|---|
| Plan | Common priority grade high; adapter QOS ra-high |
| Slurm accepted request | ra-high, priority1000, correct account/attempt |
| Allocation | One GPU, one CPU, 1 GiB host memory |
| Outcome | Native COMPLETED/0:0, API SUCCEEDED |
| GPU reservation | 72 seconds, including native runtime verification/startup |
| Durable records | One native parent, one ledger row, one FINISHED MLflow run |
| Publication | Identical result bytes in S3, authenticated API and MLflow |
| API replay | Original job under the same idempotency key |
| Cleanup | Empty native account queue; normal worker Ready |

[Sanitized measurements and checks](evidence/slurm-priority-gateway.json) retain
the complete numerical result/cost boundary and deployed source digest. This
verifies end-to-end **priority mapping**, not priority admission order between
projects. The older direct [priority-order experiment](slurm-policy.md) remains
separate evidence; combining those two observations does not establish a new
cross-project trial. Native quota/preemption authority stays with Slurm.

Focused executor/ownership tests passed (39), including mapped high/default
acceptance, unlisted QOS denial and invalid mapping rejection; Ruff passed.
This fixture's numerical correctness does not establish trained-model accuracy,
general workload portability or physical GPU utilization/energy. The broader
two-project Slurm acceptance remains open.
