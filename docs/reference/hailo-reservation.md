# Actual Hailo device allocation and quota checks

On 2026-10-03 KST, three bounded Jobs checked the existing one-device Hailo
queue. These checks exercise actual Kubernetes/Kueue/device-plugin behavior.
They do not change the failed [model quality verdict](hailo-resnet50.md).

| Case | NPU request | Actual outcome |
|---|---:|---|
| Allocated control | 1 | Non-root process opened `/dev/hailo0`; HailoRT identified HAILO8 |
| Unallocated process | None | Same image and same node; opening `/dev/hailo0` failed with ENOENT |
| Oversized request | 2, against quota 1 | Kueue reported insufficient Hailo quota; Job remained suspended; zero Pods |

The positive control matters: denial on a node without a usable accelerator
would not establish allocation behavior. Both executed Pods used the identical
image digest and UID 10001, with no privileged mode, host mounts, host network,
host PID/IPC, service-account token or added capabilities. Both used HailoRT and
the existing PCIe driver 4.23.0. No model was loaded by these probes.

The oversized request was observed twice, **64.939 seconds apart**, with the same
Job identity and a current-generation `QuotaReserved=False` condition. Its
command would immediately fail if unexpectedly admitted, without opening an
accelerator. Zero Pods means zero node-level allocation for this request;
time spent waiting is not accelerator execution time.

The allocated control added **2 NPU reservation seconds** from Pod scheduling
to container finish, or **4 seconds** from Kueue admission to observed Workload
finish. These are different accounting boundaries, not measured utilization.
The CPU-only probe consumed host resources but no requested NPU allocation.
[Raw public evidence](../evidence/hailo-reservation.json) includes both time
boundaries and all probe outcomes. Actual site identifiers remain private.

All three owned Jobs were deleted after evidence capture and UID verification.
The queue returned to zero pending/admitted/reserving workloads; its policy
remained byte-equivalent as structured data. No host/runtime upgrade occurred.

## Reproduce in an isolated lab

Use the already qualified immutable Hailo image and a dedicated queue with
one Hailo device, one CPU and 1 GiB host memory. Its ResourceFlavor must select
the actual Hailo node for **CPU and memory as well as NPU**, so the CPU-only
case does not accidentally land on a different node. The queue must not borrow
extra Hailo capacity. Preserve unrelated project/GPU quotas.

Render each case using the same image, namespace and queue:

```sh
python examples/hailo/build_access_job.py \
  --case control \
  --image "$HAILO_IMAGE_DIGEST" \
  --namespace research-lab --queue hailo-lab --name hailo-access-control \
  > control.json
kubectl create -f control.json
```

Repeat rendering with `--case unallocated` and then `--case oversize`, using
distinct names. The renderer submits nothing itself. Each Job starts suspended
for Kueue admission, disables retries, and has a 60-second active execution
limit. Suspended queue waiting is not covered by that active limit: observe and
explicitly delete the owned oversized Job after the bounded test.

1. Require the control to finish successfully and report HAILO8 before running
   the unallocated case. Save its Job/Pod/Workload UIDs and image digest.
2. Require `RESOURCE_ADVISOR_DEVICE_ACCESS` to report no opened device for the
   unallocated case. Check **actual** Pod node and image equality with the
   control. An unrelated runtime error or root execution is not a pass.
3. For oversize, save two observations at least 15 seconds apart. Require
   `spec.suspend=true`, no owned Pods and current-generation insufficient
   Hailo quota evidence. Keep any unexpected admission as a failed test.
4. Preserve complete logs/manifests and timestamps, then delete only the Jobs
   created by this invocation after checking their UIDs. Check that queue
   policy is unchanged and reservations have been released.

This is a bounded device-injection and admission check for ordinary non-root
Pods. It does not prove adversarial tenant isolation, container escape
resistance, sharing fairness, time slicing, NPU utilization, model quality,
priority ordering or the Resource Advisor API/DB/artifact path. Those gates
require their own evidence.
