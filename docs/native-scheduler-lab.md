# Native accelerator scheduling lab

Open **Console → 스케줄링 실험실** (`/console#scheduler-lab`). Select a scenario,
then **실제 GPU로 실험 시작**. The server persists a request; an external bounded
runner submits real native jobs and publishes observed snapshots every few seconds.
Closing the browser does not stop the experiment. **실험 중지** cancels this run.
The history selector and timeline replay stored observations, not synthetic progress.
For GPU and NPU workloads together, choose **GPU + NPU PoC** and its execution button.

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
