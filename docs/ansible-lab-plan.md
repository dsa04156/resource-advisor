# Ansible lab telemetry acceptance plan

This section17.2 increment provisions a dedicated, pinned node-exporter on the
two separately authorized ARM Slurm lab hosts. It does not install/upgrade
Slurm, CUDA, JetPack, PCIe firmware or Kubernetes, or qualify a GPU/NPU model.
The controller's unavailable state must not be hidden by an active slurmd.

1. Read actual architecture/Python/Slurm versions, active services and listeners.
   Refuse active kubelet/EdgeCore hosts, unapproved inventory, production targets,
   an existing unmanaged installation or occupied dedicated telemetry port.
2. Use a separate hash-locked Ansible environment. Verify the official release
   archive checksum and derived binary SHA-256. Reject modified binary bytes
   before modifying any target file. Keep private inventory/authentication out
   of the repository and enforce normal SSH host-key checking.
3. Capture hashes/existence of the original Slurm configuration and NVIDIA
   version files, slurmd process/activity and host boot identities. Run syntax
   and read-only preflight checks before installation. These are not metrics
   availability or a complete predicted install diff.
4. Install only `/opt/resource-advisor/node-exporter`, its ownership marker and
   `resource-advisor-node-exporter.service`. Bind loopback port19100, run with a
   dynamic unprivileged user and bounded CPU/memory, and enable only the selected
   CPU/memory/load/network/disk collectors. No package-manager/driver operation.
5. Read real metrics from each host, including a second independent scrape.
   Require correct binary/build identity, CPU counters advancing and available
   memory bounded by total memory. Record absence rather than claiming GPU/NPU
   utilization from system telemetry. Verify original files/process/boot state.
6. Apply the same play a second time and require zero changed tasks. Re-run the
   read-only preflight and check the service remains running with the same PID.
7. Verify unapproved inventory and modified-binary negative cases stop with zero
   host changes. Retain failure output and distinguish software negative checks
   from real host installation evidence.
8. Publish scoped setup/reproduce instructions, artifact/version locks, actual
   acceptance evidence and remaining controller/scrape-integration requirements.

Installation is limited to the new dedicated service. It remains available for
subsequent monitoring work; it is not a temporary benchmark worker. Loopback
scrapes through SSH do not prove central Prometheus ingestion or dashboard use.
