# Container exit while Job completion is pending

During the [CUDA load-drift trial](load-drift.md), several successful attempts
briefly changed from `RUNNING` back to `QUEUED` before result collection. The
adapter classified every non-running workload container as queued, including a
container whose state was already `terminated` while the Job controller had not
yet reported `Complete` or `Failed`.

This also exposed a correctness issue: the worker applies its admission-wait
deadline to `QUEUED` observations. A long-running attempt could therefore receive
a spurious `QUEUE_DEADLINE_EXCEEDED` cancellation after its container had exited.
A scheduler-double regression reproduced both the status reversal and erroneous
cancel request before the fix. The earlier hardware trial did not hit that
deadline and its validated results/accounting remain unchanged.

The adapter now keeps a running **or terminated** owned workload container in
the active `RUNNING` lifecycle until the Job's terminal condition arrives. Here
`RUNNING` includes awaiting controller acknowledgement; it is not a GPU activity
measurement. Pending/image-waiting containers still report `QUEUED`. Success and
failure continue to follow the existing Job-condition checks, and collection
does not start early from a container exit code alone. The existing retained-Pod
cancellation/disappearance path remains separate.

This follows the controller distinction documented in Kubernetes's
[terminal Job conditions](https://kubernetes.io/docs/concepts/workloads/controllers/job/#terminal-job-conditions).
Kubernetes 1.31 and later wait for Pod termination before adding terminal Job
conditions; the observations are not an atomic combined update. No new cluster
feature, upgrade, state-enum migration or scheduling policy is required.

Regression coverage includes exit codes zero/nonzero, retention enabled/disabled,
unchanged timestamps/allocation, no premature result read or ledger, no false
queue cancellation, and normal final success/failure with seven measured
allocation seconds in the fixture. These are synthetic scheduler tests,
not new hardware benchmark measurements.

```sh
uv run pytest -q tests/test_backends.py tests/test_kubernetes_retention.py tests/test_worker.py
```

The source fix must be rolled out to the trusted worker to affect live status
collection. The original ten-Job workload source and raw evidence stay immutable.
