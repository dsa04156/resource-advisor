# M2: native CPU execution on both ARM Slurm workers

## Current implementation and observation

The fixed [`slurm_cpu_workload.py`](../examples/slurm_cpu_workload.py) runs without
the control-plane package, PyTorch or accelerator SDKs. Python 3.10+ can produce
the same v1 result envelope that the API validates. It requires Linux arm64 and
the actual board/CPU identifier, exact Python version, one CPU/1024MiB/zero
accelerators, host memory, CPU-only allocation, no configuration parameters and
a Slurm allocation identity. GPU allocation/visibility and unqualified sampling
or thermal requests are rejected before computation.

The calculation is generated 64×64 scalar FP64 matrix multiplication, five
measured repetitions after one untimed warmup. Input construction is timed;
analytical output verification is outside each measured interval and checks every
element on every repetition. Host process peak RSS is reported, not GPU memory.
Missing utilization/power/temperature remain null. This is a numerical fixture,
not trained-model accuracy, accelerator performance or a scheduler comparison.

On October 7, the same source's read-only `--describe` path was evaluated over
existing authorized SSH connections on both workers. It read the actual CPU
implementer/part and device-tree board model: Orin Nano and Raspberry Pi 5, both
arm64, system Python 3.12.3 and 3.13.5 respectively. No CPU calculation,
installation, scheduler submission or live configuration change occurred.
[Sanitized descriptions](evidence/slurm-cpu-description.json) bind these reads to
the source hash. They are environment observations, not runtime qualifications.

Local synthetic-host/result tests validate numerical corruption rejection,
the platform envelope/digest roundtrip, request boundaries and refusal before
computation. Existing gateway tests separately cover the explicit zero-GRES scope.
Neither category proves real native execution or cgroup enforcement.

## Frozen execution protocol — pending controller recovery

1. Confirm the original controller is reachable through approved identities.
   Reconcile the SAME unresolved original two-project attempt and its native
   accounting first; do not resubmit or replace it. Check both selected workers'
   current scheduler readiness and allocation, existing quota/account/QOS and
   runtime/driver identities. Abort on unexplained changes or unknown allocations.
2. Install this standalone source and existing runtime guard into a new,
   operator-owned CPU directory on each allowed node. Preserve GPU runtimes,
   drivers, original gateways and results. Read `--describe` again using the exact
   installed interpreter and lock each node's environment separately.
3. Hash the guard, manifest, executable, source and relevant interpreter/runtime
   inputs. Include the deterministic `--describe` output as a manifest probe.
   Use a new variant/environment/workload identity, `image: null`, CPU device
   class and an explicit CPU-only context; never reuse GPU qualification evidence.
4. Add separate CPU SSH identities/routes using the explicit
   [CPU gateway scope](slurm-runtime.md#explicit-cpu-only-gateway-scope). The
   generic configuration remains inactive until exact guarded tails are pinned.
   Keep the current account/partition/quota policies and authorized result paths.
5. Submit exactly one new normal CPU fixture per worker through `sbatch`, with
   one CPU, 1024MiB, no GRES, one task/node and a two-minute execution limit.
   Record accepted ID immediately; do not replace a failed or observation-timed-out
   Job. Inspect the same identity and actual scheduler state on recovery.
6. Require actual `sacct` parent/step state, exit, assigned node, requested and
   allocated TRES, start/end, zero GPU/NPU allocation, complete numerical outputs
   and the independent API result/digest/identity validation. Allocation variables
   in the program are sanity checks; native accounting is authoritative.
7. Preserve both outcomes and all known CPU reservation time, including setup or
   failures. CPU core-seconds are not GPU-seconds, utilization or energy. Confirm
   unchanged original Jobs/configuration/quota and no remaining owned allocation.
   Use another explicit protocol for subsequent API/MLflow/accounting registration
   or CPU cancellation; do not claim those from this initial qualification.

The two-node computation has **NOT RUN**: the controller still does not answer.
Pi NPU detection/execution remains a separate physical prerequisite. The existing
Orin GPU successes and this CPU description do not qualify the Pi accelerator.
