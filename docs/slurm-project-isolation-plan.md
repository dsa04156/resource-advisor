# E6 two-project Slurm acceptance plan

Freeze this protocol before provisioning or new GPU submissions. This is native
policy and ownership acceptance, not a scheduler-performance or fairness study.

## Scope and unchanged conditions

Use the existing qualified Orin GPU, native sm_87 PyTorch CNN fixture, immutable
runtime guard/manifest and one CPU/1GiB/one physical GPU request. Preserve the
existing project A UID, account, route, results and all native limits. Add one
dedicated project B UID/GID on the controller and GPU worker, a separate Slurm
account/association, forced-command SSH key and mode-0700 result directory.
No sudo privileges, ordinary shell login, driver change, reboot, scheduler
reconfiguration or upgrade is part of this work. Both project accounts share
the same single physical GPU; each has a ceiling, not a reserved entitlement.

Retain the existing normal/high QOS and priority weights. B may use only the
same qualified partition and QOS; its aggregate ceiling is one CPU/1GiB/one GPU.
Do not modify root, A or unrelated associations. Native queries/cancellation
must filter account, partition and Linux owner. The existing trusted worker
may carry two explicitly scoped routes/keys; this is not process isolation
between worker routes or direct MLflow/S3 tenant authorization.

## Preconditions and bound

- Save native configuration/association/QOS and A gateway/worker identities.
- Require idle compute/outbox, healthy API/DB/artifact/MLflow delivery, idle
  Orin and unchanged qualified runtime files and protected daemon identities.
- Check new names/UID/GID/paths for collisions before provisioning. Repeated
  provisioning may update only resources with the role's ownership receipt.
- Register B-owned immutable contracts, normal/high SchedulingProfiles,
  artifact and experiment mappings; refresh A capability only with a new
  evidence-backed record if its old snapshot expired.
- Bound: one B qualification API Job, six application GPU Jobs in two rounds,
  native rejected CPU/memory/QOS/foreign-account requests, and scoped API/native
  ownership checks. Do not automatically repeat failed or timed-out Jobs.
- Native run limit stays two minutes; platform queue allowance is ten minutes.
  Keep all failed/partial attempts and allocation costs. Observation timeout
  requires inspecting the same saved IDs, not submitting replacements.

## Acceptance

1. B's qualification completes the same 400 numerical comparisons. One native
   parent, ledger and MLflow run resolve to B, with identical S3/API/MLflow bytes.
2. In both UIDs, native two-CPU, 2GiB and unassigned-QOS submissions are rejected
   by Slurm, not by a mock or an unavailable SSH transport. Cross-account native
   submission is rejected. Gateway scope escapes are separate negative checks.
3. Round 1: A normal occupies the GPU. Submit B normal first, then A high;
   observe both native jobs pending. After holder completion, A high starts
   while older B normal is still pending. All three validate.
4. Round 2 reverses projects: B holder, A normal, B high. Actual native QOS,
   priority, account, user and accounting start/end support the order. Preserve
   pending reasons; don't describe account limits as full physical CPU usage.
5. Foreign project Job reads/cancels/artifacts return 404. Foreign-owner gateway
   queue/accounting/result/cancel cannot expose or affect the other project.
   A successful scancel process alone is not proof of isolation: observe the
   target job remains pending/running with unchanged identity afterwards.
6. Retries within a project return one attempt; the same key in different
   projects creates separate attempts. Lists/usage expose only owned records.
7. Each completed API attempt has exactly one result, ledger and original
   project MLflow run. Compare result bytes and allocation records, including
   qualification. No GPU reservation intervals overlap on this physical GPU.
8. Finish with no active trial jobs/pending outbox, normal worker Ready and
   unchanged source config/QOS/A associations/daemons. Retain records/results
   and the bounded B route for further lab use; close temporary forwards.

Public evidence must omit site endpoints, credentials, Linux hostnames and raw
private reports. Publish aggregate checks, policy settings, timings and limits.
This closes the tested Slurm E6 boundary only; Pi NPU, full E7/failure matrix,
hostile tenants, global fairness and complete platform acceptance remain open.

Native contracts: [Slurm associations](https://slurm.schedmd.com/sacctmgr.html)
and [resource limits](https://slurm.schedmd.com/resource_limits.html).
