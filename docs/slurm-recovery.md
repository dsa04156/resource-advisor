# Slurm connectivity recovery — 2026-10-06

This is the earlier recovery capture, not a continuous availability guarantee.
The later [two-project trial](slurm-project-isolation.md) encountered a new
controller outage. Both worker SSH paths remain reachable; native controller
termination/accounting for the last original attempt is still unconfirmed.
The [current read-only Pi check](evidence/pi-pcie-readback-v2.json) again finds
BCM2712/RP1 PCIe devices, active Slurmd and retained Gen2 settings, but external
link-down and no detected DEEPX/Hailo endpoint. No reboot or driver/boot change
was made. Physical board/cabling/power need verification before NPU execution;
daemon presence is not a successful Slurm CPU Job.

The controller is reachable again. Both dedicated ARM workers now register as
`IDLE`, with no allocated CPUs/memory and an empty queue. The Orin worker advertises
one `gpu:orin_nano` GRES. The Pi still has no detected accelerator and advertises
no GPU/NPU GRES. [Sanitized current evidence](evidence/slurm-recovery-20261006.json)
supersedes the controller-unreachable readiness observation of October 3.

## Diagnosis and bounded recovery

TCP/SSH success alone did not establish scheduler readiness: both workers were
`DOWN+NOT_RESPONDING`, with the reason `Not responding` retained from the outage.
Worker logs included expired MUNGE credentials encoded before connectivity was
restored. Fresh credentials were then successfully decoded in both directions
between the controller and each worker. Their sampled clocks agreed within a
second; the old log entries did not justify replacing keys or changing clocks.

After checking the empty queue, zero allocations and the exact node state/reason,
the operator issued `scontrol update NodeName=<verified-node> State=RESUME` for
each of those two nodes. The controller subsequently reported both `IDLE`.
The controller configuration hash was unchanged. No daemon restart, driver
installation, reboot, broad node-state reset or Kubernetes mutation was needed.
This was a diagnosis-specific lab recovery, not an automatic remediation policy.
Never resume a node with an unexplained failure or a different administrator reason.

## Remaining work

This check created no compute Jobs and establishes no model performance or API
execution result. The Orin still needs a qualified model environment; system Python
has no PyTorch and the existing driver must be preserved. The Pi's onboard PCIe
bridge/RP1 is visible, but no DEEPX/Hailo accelerator or corresponding device file
was observed. Its physical link remains a separate prerequisite.

The controller and worker configuration files differ in accounting/JWT/priority
directives from the earlier controller-only setup. Recovery did not synchronize
them: controller-local accounting addresses must not be blindly copied to workers,
and JWT signing keys must not be distributed to compute nodes. Explicit role-aware
configuration management, scoped credentials, full Slurm API/model/result/usage
execution, cancellation/recovery and cross-user tests remain open.

The state transition follows the
[Slurm `scontrol` node-state interface](https://slurm.schedmd.com/scontrol.html).
Historical [CUDA/accounting qualification](slurm-verification.md) and
[priority tests](slurm-policy.md) remain dated evidence, not fresh model results.
