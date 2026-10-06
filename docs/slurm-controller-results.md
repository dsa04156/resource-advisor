# Real Slurm queue/resource observations and restricted SSH

The [fixed controller trial](slurm-controller-plan.md) passed against the actual
Slurm 24.11.5 lab on October 6. Two Jobs were submitted, one canceled while
pending, and the holder completed its 4,096-element CUDA check. This is a
controller/transport acceptance, not an additional model qualification.

| Observation | Idle before | Holder + waiter | Idle after |
|---|---:|---:|---:|
| Running records | 0 | 1 | 0 |
| Pending records | 0 | 1 | 0 |
| GPU allocation | 0 | 1 | 0 |
| GPU reservation headroom | 1 | 0 | 1 |
| GPU worker state | IDLE | MIXED | IDLE |

The actual wait reason was **`AssocGrpCpuLimit`**, reflecting the unchanged account
CPU quota. Do not relabel it GPU exhaustion merely because GPU headroom was also
zero. The holder recorded 31 GPU reservation seconds; the waiter had no start,
allocation or output and was canceled before execution. Holding an allocation
during a deliberate 30-second sleep is not evidence of GPU utilization. The
[raw sanitized snapshots, accounting and logs](evidence/slurm-controller-v1.json)
retain these distinctions and missing step MaxRSS.

The Pi remained IDLE and exposed CPU/memory resources with no registered GRES.
Its public node alias includes `npu`, but neither that alias nor CPU readiness
means the absent DEEPX hardware is usable.

## Source-contract fixes

The initial real idle response included a successful empty-list warning that the
old collector rejected. It also supplied `gres_used=gpu:orin_nano:0(IDX:N/A)` while
omitting zero GPU usage from `tres_used`. The corrected collector recognizes those
explicit observations, never fills missing/ambiguous allocation with zero, and
requires the exact qualified controller release and parser. `gres_drained=N/A`
is the pinned implementation's unimplemented per-GRES drain report; node-level
DRAIN/DOWN flags remain separate blockers. The
[plan links the installed-version source definitions](slurm-controller-plan.md).

Software tests cover nonempty/foreign/error responses disguised by the empty
warning, unknown releases, missing counts, duplicate typed resources, aggregate
plus typed double-counting, and retained node drain flags. These fixtures remain
separate from the actual scheduler snapshots.

## Dedicated observation credentials

The [restricted observer](slurm-observer-transport.md) is now provisioned on the
controller by the separate Ansible play. Six tasks changed on first apply and
zero changed on repeat. The authorization-negative preflight changed zero tasks.
An initial check lacked inherited private SSH variables and failed authentication
before any remote mutation; the corrected check passed. An earlier local YAML
syntax error was corrected before any apply.

Using only the new key, strict verification against the already trusted host key
and no operator password:

- The two allowed node/queue commands returned live data.
- Submit-client, cancel-client, shell and foreign-account requests each returned
  exit 1, no stdout and the forced command's rejection message. The negative
  submit/cancel requests used version/help arguments and created/canceled no Job.
- The actual collector consumed both responses successfully through this identity.
- Only allowed node names and required fields passed the transport; addresses,
  private annotations, job commands and user metadata were removed.

Controller/DBD/SSH process IDs, protected configuration hashes and boot identity
were unchanged. Both worker configurations and daemon state, existing Slurm
QOS/associations, driver hashes, node inventory and the empty queue were also
unchanged by provisioning. No scheduler association or sudo privilege was given
to the observer, and no shared operator credential enters the collector.

## Deployment boundary

**Continuous controller observations are not deployed to the console yet.**
The existing inventory image has no SSH client or passwd entry for its numeric
runtime UID. It still collects live Prometheus CPU/memory telemetry with controller
observations explicitly unconfigured. A qualified client image, mounted private
key/known-hosts, static GitOps rollout, API authorization/freshness and actual
console readback remain necessary. This report does not substitute local Python
output for that full delivery path.

The separate native PyTorch compatibility finding and complete Slurm
API→model→artifact/usage path remain open. The current controller work neither
changes drivers nor promotes the unqualified PyTorch environment.
