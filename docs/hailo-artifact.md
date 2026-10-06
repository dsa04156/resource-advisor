# Real Hailo compiled-artifact rejection

The [protocol](hailo-artifact-plan.md) was published at `05415d9` before
two new, sequential Kueue Jobs. Both completed natively with no retries or
replacement compute. [Sanitized evidence](evidence/hailo-artifact-v1.json)
records actual image/source/binding/artifact digests, admission and costs.
The reusable [verifier](../examples/verify_hailo_artifact.py) requires private
site configuration; it refuses a previously used output directory.

| Arm | Actual outcome | Usable result | NPU reservation |
|---|---|---|---|
| Qualified ResNet-v1-50 HEF | Native COMPLETED, exit0; 100 hardware outputs, accuracy80%, reference agreement97%; original gates pass | One validated hardware envelope | 5 seconds |
| Different retained ResNet-v1-18 HEF | Native FAILED, exit1; `compiled model digest mismatch` | No result envelope, qualification report or performance profile | 3 seconds |

Both used the same pinned main image, immutable ResNet50 binding/fixture and
unchanged Hailo runner. An identical pinned init container copied the existing
ResNet18 HEF into a per-Pod emptyDir; the main container mounted it read-only.
Only the HEF path supplied to the runner differed. Actual byte digests confirmed
the alternate file was the previously executed ResNet18 artifact, not an invented
error message or modified original model. The ResNet50 positive again reported
HailoRT/driver/firmware4.23.0, Python3.13.16 and NumPy2.2.6.

The executed qualification source verifies the HEF digest before Device.scan,
device configuration and inference. This is source/control-flow evidence of
the rejection boundary; this trial did not trace kernel-level device access.
Importing the SDK alone is not NPU inference.

Both requests were admitted by the existing Kueue LocalQueue with one physical
Hailo, one CPU and 1GiB. Both had restart Never, backoff0, no service-account
mount, no privilege escalation and no hostPath. The init requested one CPU,
256MiB and no NPU. Total reservation was **8 NPU-seconds and 8 CPU core-seconds**,
including initialization and the rejected arm; these are not utilization or
CPU busy time. Power/temperature/NPU memory were not measured.

These direct qualification Jobs inserted no API/MLflow/profile/usage records.
All589 application Jobs, 2,582 outbox records, 588 ledger rows and 2,637 immutable
result/profile/tracking/artifact records were independently unchanged.
Preexisting native object UIDs/specs and ClusterQueue specs were preserved.
Both test Jobs are terminal, and the queue had zero pending/admitted workloads.
The temporary database forward was closed; persistent API connectivity remains.

This closes the named different-compiled-model negative for this qualified NPU
contract. It neither qualifies ResNet18 nor proves arbitrary model conversion,
all-device support, compiler correctness or fleet effectiveness. The absent Pi
PCIe accelerator, disconnected final Slurm attempt, broader E0 and original
HAIRP scope remain open. No failed result is promoted into recommendation history.
