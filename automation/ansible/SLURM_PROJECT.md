# Dedicated Slurm lab project provisioning

`slurm-project.yml` creates one project-specific Linux UID/GID on an explicitly
authorized controller and qualified GPU worker. Supply a private inventory with
group `ra_slurm_project_hosts`; ordinary researchers do not receive this inventory
or administrator credentials. Production hosts are excluded by default.

Required variables:

| Variable | Contract |
|---|---|
| `ra_lab_authorized`, `ra_slurm_project_authorized` | Explicitly true for the dedicated lab |
| `ra_existing_production` | Explicitly false |
| `ra_executor_name`, `ra_executor_uid`, `ra_executor_gid` | Dedicated `ra-` name; unused identical numeric UID/GID on both hosts |
| `ra_executor_home`, `ra_executor_base` | New root-managed project home/code paths under the allowed prefixes |
| `ra_executor_public_key` | Independent Ed25519 public key; keep its private key outside the repository |
| `ra_executor_scope` | Existing gateway contract: user/account/partition/node/GRES/QOS/output directory, normal/high map, exact qualified native binding tails |
| `ra_executor_video_access` | Optional only on the qualified worker |
| `ra_executor_device_acl` | Optional named-user access to explicitly qualified `/dev/dri/renderD…` files |

Set scope mode `controller` on the controller, `results` on the worker. Results
use a separate mode-0700 directory owned by the project UID. Code, scope and
authorized keys are root-owned. SSH keys have `restrict` plus the forced gateway;
no ordinary shell, PTY, forwarding or sudo is granted. Existing Slurm account/QOS
names must be independently reviewed before use. The tested native contract is
Slurm 24.11.5 and one CPU/1GiB/one GPU; the role does not generalize arbitrary
resource limits or runtimes.

The controller creates only the new account and partition-specific user
association with the existing normal/high QOS. Existing QOS, root/A associations,
controller configuration, daemon processes and drivers are not changed. The
role refuses to adopt preexisting unmanaged users, numeric IDs, paths or
associations. A root-owned ownership intent precedes user creation so a partial
run can be inspected and resumed without silently adopting another identity.
Policy drift is rejected rather than overwriting an existing account's limits.

```sh
ANSIBLE_CONFIG=automation/ansible/ansible.cfg ansible-playbook \
  -i /private/slurm-project-inventory.json automation/ansible/slurm-project.yml
```

Check fresh host/GPU readiness before executing any work. Provisioning or a
zero-change repeat is not model/runtime qualification. API credentials, worker
routes, MLflow/artifact mappings and immutable profiles are separate steps.
The [two-project acceptance protocol](../../docs/slurm-project-isolation-plan.md)
defines subsequent real Job tests. Record all changes and preserve configuration/
association evidence; never delete shared accounts or result files as cleanup.
