# Native accelerator scheduling lab

Open **Console → 스케줄링 실험실** (`/console#scheduler-lab`). Select a scenario,
then **실제 GPU로 실험 시작**. The server persists a request; an external bounded
runner submits real native jobs and publishes observed snapshots every few seconds.
Closing the browser does not stop the experiment. **실험 중지** cancels this run.
The history selector and timeline replay stored observations, not synthetic progress.
The default ten-request scenario covers all registered GPU/NPU execution families.

## Current catalog: ten requests in every scenario

The default is **전체 연결 자원** (`fleet_batch`), using all registered GPU/NPU
execution families. The selector contains five ten-request scenarios; the previous
`mixed_batch` is available through history. Older runs remain in the
server's audit history; they are no longer offered as new UI scenarios.

| Scenario | Requests and native admission | Evidence |
|---|---|---|
| All connected resources (`fleet_batch`) | Five automatically routed CUDA requests (90-second repeated-kernel variant when qualified/configured), one registered CNN, two registered Hailo inferences, two Slurm Orin CNN requests | Ten ordinary Job API records, actual backend/node/result observations |
| CUDA-only policy comparison (`pool_batch`) | Ten one-GPU CUDA probes, submitted concurrently to `hairp-gpu-pool` | Concurrent running/pending jobs, native quota reason, ten CUDA correctness results |
| Quota comparison (`burst`) | Ten one-GPU CUDA probes in the isolated two-GPU comparison queue | Same request count with the original restricted quota; not total platform capacity |
| Priority batch (`priority_batch`) | Five low-priority and five high-priority one-GPU probes in the common pool | Actual native priority values and observed admission order; no invented FIFO/preemption guarantee |
| Multi-GPU groups (`gang_batch`) | Three two-worker GPU groups and seven one-GPU requests in the common pool | Thirteen worker results, whole-workload admission and application startup barriers |

The header distinguishes six registered physical GPU nodes, qualified NPU execution
routes and the cumulative nodes actually used by this run. CUDA-only comparisons
are explicitly marked; their three-node quota is not total platform capacity.
The six-stage board shows all ten requests beside a table of every inventory node.
Seeking the timeline also selects the node observation saved with that event.
The explicit current-state toggle is a different time basis. Allocation is not
physical utilization; a missing sensor remains unmeasured. CPU-only, unavailable
NPU and model-unqualified nodes stay visible with their participation constraints.

The native CUDA comparison probe is qualified for three exclusive-GPU nodes.
The default registered CUDA workload has four compatible candidates, including
a Jetson runtime; other Jetson, CNN and NPU variants use their own registered
contracts. The automatic router uses active accepted project requests and bounded
five-minute assignment history as candidate tiebreakers, serialized with a
PostgreSQL transaction lock; Kueue/Slurm retain native
admission. This is not a cross-project GPU reservation or an optimal interference
model. See [common GPU pool](common-gpu-pool.md) for configuration and limits.

### Common-pool execution record — 2026-10-07

The common-pool ten-request run succeeded on three native CUDA nodes: three running
and seven pending requests were observed together, then all ten correctness probes
finished. Fifteen synchronized node observations were saved and owned native
Job/Pod/Service objects were removed.

The mixed run completed all ten ordinary jobs: eight GPU and two NPU requests,
eight Kubernetes and two Slurm executions, ten collected results, ten distinct
MLflow run receipts and ten accounting records. Five automatic CUDA requests
selected four compatible GPU nodes. All workloads together executed on seven
nodes (six GPU nodes and one NPU node). Slurm's account CPU quota visibly queued
its second job; no scheduler limits were silently overridden.

The first mixed attempt failed because serialized acceptance exceeded the runner's
15-second receipt timeout for three requests, although the server accepted all ten.
All ten were reconciled and no nonterminal job remained. That failed history entry
is retained. The corrected runner waits up to 60 seconds and retries transient
receipt failures using the same idempotency keys; its second run succeeded.

The grouped run also succeeded: three two-worker groups plus seven one-GPU requests
produced thirteen CUDA correctness results, including six barrier observations.
Two running Job records can occupy three GPUs when one Job requests two devices.
All owned native objects were cleaned. Priority and quota-comparison definitions
were not rerun in this increment; their older evidence remains explicitly archived.

The served UI showed ten rows, six stages and all twelve nodes at 1366×768;
390px width had no horizontal overflow. Explicit Slurm display-alias mapping was
verified by selecting a completed Slurm job and highlighting its inventory row.
Initial 28 focused checks passed; the receipt-fix runner checks passed 10 tests.
Sanitized counts and boundaries are in
[the common-pool evidence report](../evidence/common-gpu-pool-ten-request.json).

### Full-fleet default and completed-pod cleanup — 2026-10-07

The new default `fleet_batch` completed ten actual jobs on six GPU nodes and
one NPU node: eight GPU requests, two NPU requests, eight Kubernetes executions
and two Slurm executions. All ten results, ten distinct MLflow run receipts and
ten PostgreSQL usage records were verified. Five automatic CUDA requests used
four compatible nodes. The board retained ten rows and twelve node observations
on one desktop screen; execution coverage is cumulative, not simultaneous use.

A preceding default run lost its HTTPS transport and failed after nine successful
jobs; its remaining Slurm job was canceled. The failed history is retained.
Bounded receipt/report retries and report-based agent liveness were added, then
the successful run above was executed. This does not prove arbitrary process-crash
recovery or unlimited network-outage tolerance.

The lab operator removed 573 historical platform terminal pods after matching
terminal database attempts and terminal parent Jobs. Another 69 terminal diagnostic
pods had their logs and Pod status archived locally before deletion. Eight of those
had an orphaned platform evidence finalizer; only that exact finalizer was released
after archival and parent completion checks. Runtime services and nonterminal jobs
were preserved. A private five-minute timer now removes terminal platform pods only
after database completion and parent Job completion. Job records, collected results,
MLflow references and accounting remain intact. It does not clean arbitrary projects.

[Sanitized full-fleet evidence](../evidence/full-fleet-ten-request.json) includes the
retained failure, actual execution counts and remaining accelerator constraints.

## Archived scenario definitions and evidence

The following describes earlier independent experiments, not the current selector.

### Previous twelve scenario definitions

Each selector contains prerequisites, four observation steps, success criteria and
the boundary of what the experiment demonstrates. The latest recorded outcome is
shown on the selector; readiness from runner configuration is not a successful run.
History entries include their terminal status. Up to 40 recent runs are returned.

| Scenario | Real execution | Required evidence |
|---|---|---|
| Ten concurrent requests | Ten independent one-GPU CUDA Jobs submitted concurrently into the existing two-GPU queue | All ten native Jobs observed; running and pending together, a native quota reason, then ten successful CUDA correctness results |
| Quota backlog | Two-GPU blocker, then three one-GPU requests; release blocker | All three observed pending, native quota shortage reason observed, all three eventually succeed |
| Priority | Two-GPU blocker; low priority submitted before high, both request two GPUs | Native high value > low, high admitted while low still waits, both succeed |
| Queued cancellation | Occupy quota, enqueue and cancel one request, release blocker, submit replacement | Owned Job deletion confirmed; cancellation retained; replacement succeeds |
| Failure / resubmission | GPU-requesting test container exits 42; submit a new CUDA Job | Native exit code 42 and successful new CUDA correctness result |
| NPU inference | Registered qualified NPU workloads only | Every NPU job succeeds through its ordinary result path |
| Kubernetes + Slurm | Registered Kubernetes GPU/NPU and Slurm GPU workloads | Both actual backend values observed and all jobs succeed |
| GPU + NPU | Registered heterogeneous workloads | Actual execution and collected result for each |
| Multi-GPU | One count-selected IndexedJob, one GPU per worker | Distinct worker results and startup barrier |
| Gang | Block one GPU before submitting two-worker group | Whole admission, both workers pass barrier and finish |
| Topology | Same-node request, then relaxed comparison | Native topology rejection reason and two-node completion |
| Backfill | Long/short Slurm jobs around finite future reservation | Native start order, completion and increased backfill counter |

The new quota/priority/cancellation experiments use the existing isolated **two-GPU**
queue. Their blocker is intentionally canceled after queue evidence is saved; its
`CANCELED` card is expected, and does not mean the experiment failed. A canceled
pending request did not hold quota: releasing the blocker returns quota. These
are requests in one lab project, not fabricated users or a fairness benchmark.
Kueue can report a quota shortage only for the request it has evaluated, leaving
other pending requests without a reason. The runner requires all requests pending
and at least one native quota reason; it does not invent reasons for other jobs.

Priority setup is additive: apply `examples/scheduler_lab/priority-resources.json`
and configure `priorities.low` / `priorities.high` in the external runner config.
The classes affect Kueue Workload ordering, not Pod priority. Existing controller
preemption settings remain unchanged. The runner rejects a success verdict unless
it observes high admission while low is still waiting.
[Kueue WorkloadPriorityClass](https://kueue.sigs.k8s.io/docs/concepts/workload_priority_class/)
documents this separation and label contract. The lab uses the already served
v1beta1 CRD; no Kubernetes/Kueue upgrade is required.

`npu` filters qualified entries from `heterogeneous`. `mixed` requires a separate
operator-configured task list containing qualified routes to both native backends.
Replace all example workload/profile placeholders before enabling these lists.
These scenarios share the runner-authenticated project restriction. They use the
ordinary Job API and its result/MLflow path, while native policy probes store
evidence in the lab record. A mixed scenario never builds one cross-scheduler DDP job.

Failure/resubmission terminates only its own test container. It does not inject a
GPU Xid, disconnect a node, corrupt a driver or restore a training checkpoint.
The expected failed child remains visible alongside the successful new child.
An unexpected error in any other child still fails the experiment.

Suggested hands-on sequence: **NPU → GPU+NPU → quota → priority → cancel → recovery
→ Multi-GPU → gang → topology → mixed → backfill**. Run one at a time. For each:
read the guide, inspect the current resource rail, press the scenario launch button,
click a waiting card for the native reason, then follow admission/execution/result.
Finish by replaying the saved observations. Short states may fall between polls;
the UI does not invent them. No destructive hardware-failure scenarios are included.

### Scenario pack execution record — 2026-10-06

All six newly added scenarios were executed against real lab resources and reached
`SUCCEEDED`, with their cleanup recorded as completed:

- **Quota:** three one-GPU requests were simultaneously pending while the two-GPU
  blocker ran. The first evaluated request had the native insufficient-quota reason;
  the other reasons were unreported. After releasing the blocker, all three CUDA
  requests completed.
- **Priority:** low priority **10** was submitted before high priority **100**.
  After releasing the blocker, high was admitted while low remained pending.
  High finished, then low ran and finished. Both retained native priority evidence.
- **Cancellation:** the queued request's Job deletion was confirmed and its canceled
  card retained. The blocker was separately released; the replacement completed.
- **Failure/resubmission:** the first container exited **42**, its failed Job stayed
  visible, and a new Job completed the CUDA numerical correctness probe.
- **NPU:** the qualified Hailo-8 ResNet-50 workload succeeded with collected results.
- **Mixed:** Kubernetes CUDA, Kubernetes CNN, Kubernetes Hailo inference and Slurm
  Orin CNN all succeeded, with both backend identities and four results observed.

The first quota attempt was canceled after discovering that Kueue does not attach
a scheduling reason to every pending request. That record remains in history;
the subsequent run used the corrected evidence condition and succeeded. A regression
check preserves missing reasons rather than synthesizing them. Seventeen focused
API/runner/console checks passed initially; all four runner checks passed after the
quota fix. Browser checks covered all eleven four-step guides, live polling with
an open guide, priority cards, desktop rendering and 390px width without overflow.
Raw observations and screenshots remain private. Existing gang/topology/backfill/
multi-GPU/heterogeneous results above are prior runs, not rerun claims for this pack.

## What the three experiments actually demonstrate

| Scenario | Native mechanism | Evidence required for success |
|---|---|---|
| Gang admission | Kueue admits a two-pod Indexed Job as one workload after a one-GPU blocker frees quota | Native pending reason, whole PodSet admission, both CUDA workers passing their application barrier and completing |
| Topology-aware | Kueue ResourceFlavor + Topology with `kubernetes.io/hostname` | Same-node two-GPU request remains pending on two one-GPU nodes; comparison job without that locality constraint receives a two-domain topologyAssignment and completes |
| Backfill | Existing Slurm `sched/backfill`, requested walltimes and a bounded future reservation | A later short GPU job runs while the earlier long GPU job is pending; both complete with short.Start < long.Start; reservation and sdiag snapshots retained |

The GPU payload is a real CUDA numerical correctness probe (4,096 squares), not a
model training benchmark. Each worker initializes a real visible GPU. There is no
CPU fallback. The two-worker barrier coordinates compute startup; Kubernetes pod
creation itself is **not atomic**. This does not implement NCCL collective training,
preemptive gang time slicing, rack optimization, NVLink awareness, or proof of an
optimal policy. Topology comparison deliberately cancels its own unschedulable
same-node job after saving native evidence, then submits the relaxed comparison.

Slurm uses a one-minute reservation starting about two minutes in the future.
The earlier job requests two minutes; the later job requests one minute and a
lower priority via nice. Both compute for around ten seconds. Requested walltime,
not measured kernel runtime, drives the backfill example. This artificial reservation
creates a repeatable lab window; it is not evidence of real multi-user utilization
gains. Reservation expiry bounds disruption even if the runner dies.

## Separation from ordinary research jobs

`/api/v1/compute/scheduler-labs` accepts only an allowlisted scenario name. A user
cannot send shell commands, select arbitrary hosts, edit queue quotas, or request
root operations. Admission for one active lab experiment is serialized across
projects. Idempotency keys preserve retries; listings/cancellation are project
scoped. Operator-only heartbeat/report endpoints feed PostgreSQL records. A local
file lock enforces one runner; this is not a distributed HA worker implementation.

Lab records are separate from ordinary WorkloadSpec jobs: their multi-worker native
semantics are not silently flattened into the existing single-worker model. Existing
MLflow/Kubeflow workflows remain available, but these three lab runs do **not** create
MLflow runs or KFP pipelines automatically. Their evidence lives in PostgreSQL.

The UI shows real job state, pending reasons, GPU placement and native admission
JSON. Slot occupancy is the current experiment allocation, **not GPU utilization**
or total cluster capacity. Completion does not imply the GPU is unused by other
projects. Times are observed transitions; start/end from Slurm remain native values.

## Installation in an isolated lab

Verified target: Kubernetes 1.31.14, Kueue 0.19.5, Slurm 24.11.5. No runtime or
scheduler upgrade was needed. Recheck your versions and device-plugin resources.
TAS must already be available; do not enable new controller features on a production
cluster just to run the example. NVIDIA runtime handler must exist on both nodes.

1. Use two expendable GPU nodes, each advertising `nvidia.com/gpu: 1`, with a
   Python image pinned by digest and qualified for both GPU drivers/architectures.
   Label those nodes `hairp.io/scheduling-lab=gpu`. No other labels are changed.
2. Review/apply `examples/scheduler_lab/resources.json`. It creates a dedicated
   namespace, queue, flavor, hostname topology and NVIDIA RuntimeClass. Existing
   queues, quotas, Slurm accounts and priorities are not reconfigured. Dedicated
   quota does not create exclusive hardware ownership: other queues can compete.
3. Create ConfigMap `hairp-lab-probe` in that namespace from
   `examples/scheduler_lab/gpu_task.py` and `src/resource_advisor/cuda_probe.py`.
4. Run the normal schema-init command, or create just the additive `runs` and
   `agent` tables exported by `resource_advisor.scheduler_lab`. Upgrading the API
   image alone does not migrate an existing database.
5. Copy `config.example.json` outside the repository. Supply an external API
   credential file with `api_url` and `operator_token`, CA, kubeconfig and qualified
   image. Credentials never enter the browser or GPU job.
6. Implement the site-specific Slurm transport. It reads JSON `{argv, stdin}` on
   standard input and returns `{returncode, stdout, stderr}`. It needs sbatch,
   squeue/sacct, scancel, sdiag, and narrowly scoped permission to create/delete
   reservations prefixed by this lab run. Submit compute as the lab account/user;
   do not grant API users arbitrary root shell access. SSH authentication is external.
7. Run `python examples/scheduler_lab/agent.py /path/to/private/config.json` under
   a supervisor with the right PATH (`rtk`, kubectl, Python) and a single writable
   lock file. Explicit kubeconfig avoids selecting a different cluster under systemd.

## Failure handling and limits

Absent/stale runner heartbeat disables new submissions. The runner has an eight-minute
experiment deadline, per-command timeouts and bounded GPU job lifetimes. Failure,
cancellation and restart attempt cleanup of only the run-labelled Kubernetes jobs
and services, recorded Slurm IDs and exact reservation name. A restarted active run
is failed and cleaned, not silently resubmitted. Cleanup errors remain failed evidence
requiring operator attention. Native completed logs/conditions are captured before
cleanup; raw infrastructure identities remain in private state, never in this repo.

An unadmitted Kueue Job deadline does not run while suspended; if the runner and
cluster are both unavailable, an operator must remove suspended lab jobs after
recovery. This runner is for a controlled lab, not arbitrary public multi-tenant
execution. Kueue controller-wide waitForPodsReady, production quota borrowing and
scheduler preemption settings are unchanged.

## References

- [Kueue 0.19 topology-aware scheduling](https://kueue.sigs.k8s.io/v0.19/docs/tasks/run/topology_aware_scheduling/)
- [Kubernetes Indexed Job communication](https://kubernetes.io/docs/tasks/job/job-with-pod-to-pod-communication/)
- [Slurm scheduling configuration](https://slurm.schedmd.com/sched_config.html)

## Recorded lab execution — 2026-10-06

All three real scenarios reached `SUCCEEDED`; this is a bounded lab observation,
not a production qualification or a statistical performance comparison.

- **Gang admission:** one GPU blocker ran first. Kueue reported
  `insufficient unused quota for nvidia.com/gpu ... 1 more needed` for the two-GPU
  group. After release, RTX 5060 Ti and RTX 5080 workers both recorded
  `BARRIER_RELEASED`, `COMPUTE_STARTED`, and `COMPUTE_FINISHED` with CUDA correctness
  checks. The native PodSet assignment reserved GPU count 2.
- **Topology:** the required hostname request reported
  `topology ... allows to fit only 1 out of 2 pod(s)`. After cancelling that owned
  request, the relaxed comparison completed on two different nodes; Kueue recorded
  a hostname `topologyAssignment` with two domains.
- **Slurm backfill:** the long request was submitted first. Native controller times:
  short **09:15:45–09:15:58**, reservation **09:17:15–09:18:15**, long
  **09:18:15–09:18:28**. Both requested one GPU and completed. The native
  `Total backfilled jobs (since last slurm start)` counter increased **8 → 9**
  when the short job entered. Accounting plus the reserved gap demonstrates native
  backfill in this run; it is not inferred solely from reversed completion order.

Afterward the dedicated Kubernetes namespace had no experiment jobs/services;
Slurm had no pending/running jobs and no remaining reservations. The dedicated lab
queue/flavor/topology and probe ConfigMap remain available for the next request.

Two setup failures remain in history: a supervisor initially picked the wrong
kubeconfig (fixed with explicit external configuration), and a four-minute Slurm
request exceeded the existing two-minute QoS (fixed by reducing the lab request;
QoS was not relaxed). The successful Slurm run used the two-minute request.
The subsequent collector reads live pending reasons from squeue and terminal times
from sacct. This collector adjustment does not justify another GPU run.

Focused validation: 10 API/console tests passed, JavaScript syntax and changed Python
lint passed. Desktop layout, 390px mobile width without horizontal overflow, job-node
selection and historical snapshot seeking were checked against the live console.
Raw observations and screenshots are private, excluded from the public repository.


## Multi-GPU PoC: one job, configurable physical GPU count

The **Multi-GPU PoC** selector accepts **1, 2 or 3 GPUs** in the current lab.
The request contains a count, not device names. Kueue assigns an Indexed Job with
one real GPU per worker; the multiarchitecture image runs on x86 and ARM64 nodes.
A bounded preceding job occupies `pool capacity - requested count + 1` GPUs for
multi-GPU requests, so the group must wait for whole admission. A one-GPU request
runs directly. The browser displays requested count, native PodSet admission,
observed GPU initialization and successful worker results. Worker cards identify
rank, node, actual CUDA device, CPU architecture and completed checks. They select
the corresponding worker evidence; recorded snapshots remain seekable.

This is **independent CUDA probes coordinated as one multi-worker job**. It does not
claim partitioned model training, gradient synchronization, NCCL AllReduce, or linear
speedup. Jetson time-slicing slots are not counted as additional physical GPUs.
Slurm is not combined with Kubernetes into a single distributed compute job.

Setup is additive: keep the original two-GPU lab pool and apply
`examples/scheduler_lab/multi-gpu-resources.json`, label the three qualified physical
GPU nodes `hairp.io/multi-gpu-poc=true`, and add the external `multi_gpu` config section.
The image digest must resolve to the required amd64 and arm64 manifests, and the
NVIDIA runtime handler must work on all selected nodes. Existing queues are unchanged;
these pools can share hardware, so quotas do not imply exclusive ownership. The API
rejects requests above the configured PoC capacity and detects changed GPU counts
on idempotent retries. Do not raise the configured limit without qualifying the
additional physical resources and adjusting the dedicated lab quota.

The current 3-worker preflight completed on RTX 5060 Ti, RTX 5080 and GB10. Each
worker emitted GPU_READY, BARRIER_RELEASED, COMPUTE_STARTED and COMPUTE_FINISHED.
The GPU payload and architecture are observed from CUDA, not inferred from UI labels.

Browser-launched 3-GPU PoC also completed on 2026-10-06: a one-GPU preceding job
caused the requested three-worker group to wait with native `1 more needed` quota
evidence. Kueue then admitted count 3, all three CUDA workers passed the startup
barrier and completed, and the API recorded `workers_passed=3`, `nodes_used=3`,
architectures `amd64` and `arm64`. Owned Job/Service resources were cleaned. The
browser verified per-worker evidence, 0/3 → 3/3 counters, pending-state replay and
390px layout without horizontal overflow. Two focused contract tests passed,
including rejecting GPU count above capacity and changed-count idempotency retries.
The count-1 and count-2 options were not separately replayed as new full experiments.

발표 시연 순서: **스케줄링 실험실 → Multi-GPU PoC → GPU 3개 → 실행**.
대기 중에는 네이티브 quota 사유를, 입장 후에는 worker별 실제 GPU 모델과
아키텍처를 보여주세요. 마지막에는 3/3 결과를 확인하고 타임라인으로 대기
시점에 돌아가면 됩니다. 다시 실행하지 않아도 완료한 기록으로 설명할 수 있습니다.

## GPU + NPU PoC: registered workloads, normal scheduler routing

Configure `heterogeneous` with existing, qualified workload/profile references.
The runner submits these through the ordinary Job API without selecting a physical
device. Each workload retains its compatible runtime, native queue, result collection
and experiment tracking. The UI displays each actual job's state, placement, pending
reason and result link. Device registration alone does not qualify a model for this
scenario: other discovered NPUs remain visible with their reported allocation and
missing-template explanation.

This scenario is restricted to the authenticated runner's project. Its configured
operator credential must not submit jobs on behalf of an unrelated project. Known
child job IDs are persisted after submission; cancellation requests their ordinary
API cancellation and waits for terminal states. Unconfirmed cleanup remains an error.
A crash or lost response between acceptance and recording a child ID leaves a small
orphan-observation window. Existing ordinary job limits bound execution; this is not
an exactly-once transaction or a highly available orchestration service.

On 2026-10-06, a browser-launched run completed three real jobs: CUDA on RTX 5060 Ti,
CNN inference on RTX 5080, and ResNet-50 on Hailo-8. All three reported `SUCCEEDED`
with collected result objects. These are independent workloads on compatible
accelerators, not one GPU/NPU distributed training group. Different models and
measurement boundaries do not establish a cross-device performance ranking.

## Automatic playback and live updates

The resource watch and execution board share one screen. Current inventory stays
visible above four lanes: submitted/pending, admitted/preparing, running, terminal.
Cards move only when recorded native state changes (or when seeking history), with
reduced-motion support. Click a card to inspect its reason and highlight observed
execution nodes; click a resource for detailed meters. A routing target before
execution is not treated as an observed allocation. Lane counts cover this experiment,
not every cluster job or a guaranteed queue ordering. Polling can skip short-lived
states; no synthetic admission event is inserted to fill those gaps.

The resource rail shows current inventory time, reservations/allocatable units,
accelerator utilization where measured, CPU use and memory headroom. GPU/NPU/Slurm
filters help inspect the heterogeneous fleet. Its current observations remain
separate from historical experiment playback. Resource overview refreshes every
15 seconds when automatic refresh is enabled; experiment observations every three
seconds. Existing freshness rules hide stale metric values.

UI references (design patterns only):
- [Run:ai dashboard analysis](https://docs.run.ai/v2.18/platform-admin/performance/dashboard-analysis/): resource allocation versus utilization and drilldown.
- [KueueViz](https://kueue.sigs.k8s.io/docs/tasks/manage/enable_kueueviz/): queue, workload and allocation monitoring.
- [Slurm-web](https://slurm-web.com/): native HPC node and queue operations.

The moving observed-state cards are a HAIRP presentation choice, not a claim that
these references implement the same animation or support all local NPUs.

Completed history automatically plays once when opened. Pause, seek and select
0.5×, 1×, 2× or 4× speed; 1× advances one recorded snapshot every two seconds.
Intervals are compressed for presentation: use recorded timestamps for execution
duration. Playback stops at the end, when leaving the page or when the tab is hidden.
Newly launched runs stay live rather than rewinding on completion.

The banner distinguishes historical playback from live polling every three seconds,
and shows the last successful response time. **최신 기록으로** exits playback and
requests fresh data. Network requests time out after eight seconds; stale API or
runner heartbeats disable launch. Focusing a selector no longer blocks live rendering.

The lab's former long-running `kubectl port-forward` could remain active while its
streams timed out, causing HTTPS and runner heartbeat stalls. The private lab host
now uses a systemd socket and `systemd-socket-proxyd` TCP passthrough to the stable
Kubernetes Service TLS port. The public-facing URL and TLS termination are unchanged.
This requires host-to-Service routing and is a lab access arrangement, not a general
production ingress. Recreating the Service with a different ClusterIP requires an
operator to update the private proxy destination. Host addresses and unit files are
kept outside the public repository.

Focused verification covered automatic advancement, speed change, pause, returning
to live mode, and response timestamps changing while a selector retained focus.
The same HTTPS endpoint responded after API pod replacement without reconnecting
the proxy. Desktop and 390px layouts were inspected. Eleven existing focused
API/console tests passed; after adding the project-isolation regression, all three
scheduler-lab tests passed. Raw run evidence and screenshots remain private.

### Completion observation gap

When every expected native worker pod has succeeded but the Job controller has
not yet reported `Complete`, the observer emits `FINALIZING`. The card stays in
the execution lane with “실행 마무리 중” until terminal Job evidence arrives.
Previously the remaining Kueue admission flag caused this interval to appear
as allocation again. Recorded snapshots with that exact pod evidence receive
the same display mapping without rewriting their raw state. Pending or retrying
workers do not qualify; real requeues remain visible. Slurm `COMPLETING` and
platform result `COLLECTING` also belong in execution, not preparation or queue.

Verification: eight runner cases passed; all four affected transitions in the
existing private run history mapped to the execution lane. The priority replay
was inspected in the browser at the affected event. No GPU rerun was needed.

### Replay node observations (2026-10-06)

Each native observation now saves the latest authorized inventory for that
experiment's project into PostgreSQL. Node readiness, scheduler reservations,
CPU, memory and available accelerator telemetry retain their source timestamps
and freshness evaluated at the observation time. Resource-only changes add a
replay event at most once per 15 seconds when no job-state event was emitted.
The existing limit of 100 recent events remains; this is bounded lab history.
Source collection is asynchronous: this is the latest available evidence at the
event, not a claim that every exporter sampled simultaneously.

The default resource rail follows the selected recorded event. **현재 상태 보기**
switches to current inventory; **실험 시점 보기** returns to recorded evidence.
GPU, NPU, CPU-only and Slurm nodes are available through the filter. A node opens
a modal with that observation's detailed readings and original collection time.
Historical freshness is frozen at the recorded time; stale or unavailable
measurements at that time stay unknown. Old experiments have no node history
and explicitly say so; their resource states are never reconstructed from today.

Scenario selection and launch share one row. Playback/seek sits directly above
node state and the four workload lanes. Guides, event logs, worker proof and
native evidence are collapsed by default. Lanes scroll locally when many jobs
are present, without squeezing job cards. At 1366×768 the live node rail and
workload board fit in the viewport; at 390px the page has no horizontal overflow.

One additional real quota run succeeded and cleaned up its native resources.
All 15 events had project-scoped node observations (10 Kubernetes and 2 Slurm
nodes); all observation timestamps matched their corresponding events. Forty-four
node-metric series changed across the saved records. Browser replay showed an
early snapshot's original meters after later observations arrived, and historical
node detail stayed available. No missing accelerator telemetry was fabricated.
Sixteen focused scheduler-lab/console tests passed, including project isolation,
latest-record selection, stale hiding and immutable historical readings.
Raw node identifiers, timestamps, screenshots and exporter values remain private.


## Ten-request operations board

Select **10개 동시 요청** and launch once. This submits ten independent native
Kubernetes Jobs concurrently; it is not a ten-GPU gang. Each requests one GPU,
500m CPU and 256 MiB memory, with a bounded 20-second CUDA probe, no retries and
a 180-second native active deadline. The existing two-GPU quota is unchanged.
The eight-minute experiment deadline and run-label cleanup apply to partial
submission failure, cancellation and restart. Success requires native quota
backlog while another request is running, and a successful COMPUTE_FINISHED
numerical result for each of the ten requests.

The board uses one fixed row per request and six columns: reception, waiting,
allocation, preparation, execution and result. All ten rows remain visible;
there is no scroll inside stage lists. Column counts describe the selected
observation, not queue rank. Admission without an assigned Pod is allocation;
assigned but not running is preparation. Running and finalizing remain in
execution. Terminal failures and cancellations remain in result. Short states
can occur between samples and are never invented for an animation.

Select a row to link its observed execution node and native evidence. The node
meters follow the same recorded observation during replay; current inventory
is an explicit separate choice. GPU, NPU and Slurm scenarios share the six-stage
board. The ten-request scenario specifically uses the qualified CUDA lab pool;
it does not assert ten distinct models, NPU qualification or FIFO fairness.

### Verified ten-request run

The new real run succeeded: ten distinct native Kueue workloads, **two running
and eight pending** in the recorded queue observation, then ten successful CUDA
numerical results. All ten Job creation timestamps fell in the same second
(native timestamp precision is one second). There were 21 recorded node-state
captures. After cleanup, zero owned Jobs, Services or Pods remained.

The deployed desktop board showed all ten rows and six stages at 1366×768; its
bottom was 717px and the collapsed page bottom 767.4px, without horizontal
overflow. Live selection linked the observed execution node; the 390px mobile
board also retained six columns and ten rows without horizontal overflow. Mobile
uses normal page scrolling for controls and node details. See the sanitized
[count and layout evidence](../evidence/ten-request-native-run.json). This proves
this bounded queue demonstration, not performance superiority or physical GPU
exclusivity.

### Agent transport continuity

A transient TLS EOF during the first default fleet run caused the old agent process
to exit; restart cleanup stopped its remaining registered jobs. The failed run is
retained. Observation/heartbeat/report RPCs now retry transient transport failures
three times, preserving certificate verification and failing client errors promptly.
A lost final report stays in memory for acknowledgement retry; the process does not
restart for that transport error. A real process crash still uses bounded cleanup,
not undocumented native-job resumption. Job acceptance retains its own same-key
three-attempt limit; network retries do not create extra execution attempts.

Authenticated agent reports also refresh the liveness timestamp, preserving the
advertised capability body. Long concurrent acceptance no longer appears offline
while fresh reports are arriving. This does not refresh hardware qualification.

### Execution length and visible queue transitions

The bring-up probe calculates 4096 integer squares only ten times, so its device
compute finishes in milliseconds. A completed row is not evidence of a long AI
training workload. `cuda_probe --sustain-seconds 90` instead runs real synchronized
CUDA kernels continuously in ten nine-second measurement windows, validates all
4096 outputs and reports actual window progress and launch count. It has no sleep
padding or invented progress. Timed windows have a separate workload/config digest
and measurement boundary; they are not single-kernel latency measurements or AI
model throughput. GPU utilization can remain low for this small launch-bound probe.

Register the timed workload only from matching new immutable images and actual
reserved qualification reports using `examples/build_cuda_target.py`, then combine
compatible targets as `cuda-sustained-auto-v1`. The runner accepts that configured
reference alongside the older smoke reference. Original runtime images and old
execution records remain unchanged. The other registered CNN/NPU workloads retain
their actual work sizes and may complete faster; Slurm jobs still follow their
native quota and execution time. Longer compute does not guarantee a particular
queue wait when resources are free.

### Bounded history reads and acceptance observations

`GET /scheduler-labs` returns metadata for up to forty runs and the full replay
for the latest run only. Other rows have `summary_only:true`; selecting one loads
`GET /scheduler-labs/{ref}` with the same project restriction. Stored history and
replay are preserved. This avoids decoding and serializing forty full resource
histories on every three-second poll. A real API Pod was OOMKilled at the former
512Mi limit during concurrent full-history polling; its interrupted run is kept.

Accepted requests are now observed from native Job views during remaining batch
acceptance, rather than holding every accepted row at its receipt state until all
ten responses arrive. Unaccepted rows remain visible. Idempotent observation/report
RPCs retry up to six times with two-second gaps; job submission retains its identical
idempotency-key retry contract. This is bounded recovery, not an availability SLA.

Independent registered requests keep their results when another submission is
rejected. Rejected rows remain visible with the server reason and no native Job
identity; they have no Job-detail link. Accepted requests continue until completion,
then the run reports its partial failure. Rejection is not native queue admission
or an executed NPU workload. This does not yet make native execution failure of
an accepted request independent of the runner's broader cleanup policy.

### Observed timed fleet run

The default fleet workload now selects the separately qualified
`cuda-sustained-auto-v1`. Five requests each completed about **90 seconds of
actual CUDA work**. The recorded board included **five running and two queued**
requests; later queued work started after capacity was released. Across the run,
five GPU execution nodes and 28 node-state snapshots were observed.

Ten requests were made, but the NPU node was unavailable: two requests were
rejected before native job creation. The remaining eight completed successfully
(six Kubernetes, two Slurm), with eight result records, distinct MLflow run
receipts and PostgreSQL usage records. Overall status correctly remains FAILED
for the two rejected requests. The accepted jobs were not canceled because of
those rejections. Completed lab Pods were removed while results and usage stayed.

The final API Pod had zero restarts and a recorded peak of 236.79 MiB under the
existing 512 MiB limit. Earlier interrupted attempts are retained; they are not
counted as successful demonstrations. See the sanitized
[timed execution evidence](../evidence/sustained-fleet-ten-request.json).

### Variable arrivals, GPU groups and bounded recovery

`adaptive_batch` creates ten real Kubernetes Indexed Jobs at generated 3–9 second
arrival gaps. The seed, planned relative arrivals and observed receipt times are
recorded; this is a generated reproducible arrival scenario, not ten real people.
Three requests ask for two GPUs and seven for one. Two-GPU work is two independent
CUDA workers, one GPU each, with an all-worker startup barrier; it is not DDP.
Each worker computes continuously for 30 or 60 seconds without sleep padding.

Low/high admission classes map to existing Kueue priorities 10/100. Pending low
requests waiting at least twenty seconds can be promoted to 100 by a guarded
Workload priority patch. Owner UID and resourceVersion checks protect concurrent
admission; already reserved work is not changed. Queue rows follow observed
priority and receipt time, with label/card animation and reduced-motion fades.
Rows retain logical request identity across native attempts. This displayed order
does not guarantee admission ahead of a smaller resource-fitting request.

One isolated request exits with explicit injected code 42. After its native Job
fails, the scenario retains its Pod/node/exit evidence and creates at most one new
attempt on another Ready, qualified GPU node. The new Job has both a node exclusion
affinity and a selected alternate hostname, so the original node cannot be used.
Native queue admission and CUDA results remain required. Unrecoverable or repeated
failures remain failures while other independent requests continue. Cancellation
and the existing eight-minute deadline retain run-scoped cleanup.

This increment exercises the existing compatible CUDA worker group and Kueue.
It does not add automatic retry to every registered Job, Slurm or NPU execution,
change production node settings, or claim checkpoint recovery of model training.

Observed run: **ten requests succeeded**, seven requesting one GPU and three
requesting two GPUs. Thirteen worker outputs passed the CUDA check; each measured
at least its 30/60-second compute target. Ten different first Job creation times
spanned 58 seconds. Six pending Workloads changed from priority 10 to 100. The
injected failure retained exit code 42; its second native attempt succeeded on a
different GPU node. Total observed run time was 218.1 seconds, with 52 node-state
captures. Eleven native attempts were retained in replay evidence and all owned
Jobs/Pods/Services were removed after completion.

The deployed desktop shows ten request rows beside twelve node rows; both fit
1366×768. Actual recorded replay verified label and card motion. Reduced motion
produced opacity feedback without transforms. See the sanitized
[variable-arrival GPU evidence](../evidence/adaptive-gpu-arrivals.json). Native Job
creation times are authoritative for receipts; planned arrival gaps do not promise
exact acceptance times during native API and observation overhead.
