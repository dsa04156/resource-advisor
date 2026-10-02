# Slurm accounting and GPU qualification — 2026-10-02

The separate lab scheduler now has SlurmDBD-backed accounting. A real GPU-reserved
job on an Orin Nano executed a CUDA kernel and verified all 4,096 output values.
The service's status adapter read its completed accounting record with explicit
UTC timestamps. [Sanitized evidence](evidence/slurm-f0.json) records the actual
result; this does not yet prove the complete Compute API → Slurm → model-result path.

## Accounting preparation

The installed scheduler was 24.11.5. The distribution's default SlurmDBD package
was older, so a matching 24.11.5 package from the existing build was used. Package
simulation showed no removals or upgrades. MariaDB and SlurmDBD were installed;
a separate accounting DB/user and local-only database connection were configured.
An empty queue was verified before each controller restart.

The first new-DB connection exposed a mismatch between the existing controller's
saved cluster ID and the newly registered accounting cluster. The controller was
restored to its original configuration and verified UP before proceeding. For this
intentional transition to a new empty accounting database, the cluster identity
file was backed up while the controller was stopped, then reinitialized. Node,
job and reservation state files were retained. Never apply this procedure blindly
to a shared state directory or an existing production accounting database.

A lab account/user association was created. A CPU smoke job completed and appeared
in `sacct`; then GPU qualification was submitted with one typed GPU GRES. Admission
enforcement remains `none`. Creating accounts is not proof that quota or priority
is enforced. The existing two-node compute partition remained present and returned
to idle after verification.

## Real GPU execution

The first GPU attempt failed because the CUDA toolkit compiler was absent. The
failed allocation and exit code remain in accounting. No toolkit was installed over
the node's existing driver environment to hide this failure.

The independent [driver-only probe](../examples/cuda_driver_probe.py) needs Python
and the CUDA driver library. It loads a small PTX kernel, allocates GPU memory,
launches 16 blocks, synchronizes and copies back the GPU-generated sequence. Only
an exact reference match produces PASS. There is no CPU execution fallback and no
performance value. The source digest was checked after transfer to the worker.

The new job completed with exit `0:0` and `gres/gpu=1` in Slurm accounting. CUDA
reported one device and driver API 13020. F0 confirms runtime/kernel functionality,
not model accuracy, PyTorch readiness, training support or device isolation. PyTorch
is absent on this node and remains a separate qualified-runtime requirement.

## Time and remaining gates

`sacct` normally printed timestamps without an offset. The adapter now requests
`TZ=UTC` and an offset-bearing `SLURM_TIME_FORMAT` for each accounting command.
The real read and regression test verify aware timestamps. Unqualified timestamp
values still remain unknown; the adapter does not assume the host timezone.

This kernel completed within one scheduler timestamp second. Equal start/end
timestamps do not establish zero GPU consumption or subsecond timing accuracy.
`JobAcctGatherType` is not configured, so actual task CPU/memory utilization is not
claimed. GPU reservation is not GPU utilization.

Open work: qualified model environment/identity enforcement, API submission and
result transport across non-shared worker filesystems, scoped SSH credentials,
Slurm account/QOS enforcement and priority demonstrations, job/step utilization,
device cgroups, failure/cancellation recovery and PostgreSQL usage integration.
The NPU node remains unqualified until its physical device and runtime/model path
are independently verified.

References: [Slurm accounting](https://slurm.schedmd.com/accounting.html),
[resource limits](https://slurm.schedmd.com/resource_limits.html), and
[CUDA driver kernel launch](https://docs.nvidia.com/cuda/cuda-driver-api/group__CUDA__EXEC.html).
