# ResNet-v1-50 on Hailo-8

The single qualification prescribed by the [frozen plan](hailo-resnet50-plan.md)
passed all three gates on its disjoint 100-image subset. This qualifies this
specific compiled artifact, fixture and runtime, not arbitrary Hailo models.

| Measurement | Observed |
| --- | --- |
| NPU top-1 label accuracy | 80/100 |
| Original TFLite reference accuracy | 79/100 |
| Unrestricted NPU/reference top-1 agreement | 97/100 |
| Accuracy loss | −0.01; maximum allowed 0.05 |
| 100 synchronous inference calls | 0.339087234 s |
| Median / p95 call latency | 3.384091 / 3.421942 ms |
| Host process peak RSS | 128.15625 MiB |
| Scheduled → container finished allocation | 8 s × 1 NPU |
| Queue creation → admission | 0 s at scheduler timestamp resolution |

The 100 calls follow four warmups; transfers are within the synchronous host
float32 NHWC → NPU output call boundary. Download, reference calculation, image
build, container startup and result collection are outside that timing. NPU
memory, utilization and power remain unknown. The accuracy subset is class
balanced Imagenette, not a full ImageNet accuracy estimate. Earlier ResNet18 and
EfficientFormer failures remain unchanged and their causes remain unproven.

Evidence: [raw predictions](evidence/hailo-resnet50-qualification.json),
[input manifest](evidence/hailo-resnet50-inputs.json),
[scheduler provenance](evidence/hailo-resnet50-provenance.json).
HailoRT, PCIe driver and firmware are 4.23.0; Python 3.13.16, NumPy 2.2.6.
The manifest includes the original model and per-image tensor/reference hashes.

## Platform runner

[`hailo_benchmark.py`](../src/resource_advisor/hailo_benchmark.py) checks the
immutable image binding before device acquisition, then checks actual runtime
and result identity. The binding fixes the model, HEF, fixture, shape, seed,
precision, one physical NPU, one host CPU and 1 GiB host memory. The only allowed
modes are observation and fixed execution. No CPU inference fallback exists.

Every execution recomputes all three quality gates from predictions. A failed
agreement or accuracy-loss gate emits a bound `FAILED` result with **no usable
performance measurements**, even if accuracy alone passes. A successfully
emitted envelope gives process exit zero so the platform can collect and record
the failure. Configuration/runtime errors stop the process before any successful
result can be emitted.

`peak_memory_mib` is explicitly **host process RSS** for this workload; the
workload signature contains that memory boundary. It must not be compared to
CUDA allocated-memory measurements. Precision `hailo-compiled-float32-io`
describes the interface, without claiming uniform precision inside the HEF.

The standard result bundle carries identity and measurements. Full per-image
reports are emitted separately for archival and operator import into the
qualification registry; that import alone never grants execution rights.
## Completed API integration

The three observation Jobs and one independently approved verification Job all
completed on the same physical Hailo-8 through API → dedicated Kueue queue →
KubeEdge → result validation → PostgreSQL → S3/MLflow. Every Job reproduced
80/100 accuracy and 97/100 reference agreement. This is assistant-executed lab
evidence, not a claim of manual operation by the repository author.

| API execution | 100 timed calls (s) | Host peak RSS (MiB) | NPU allocation (s) |
| --- | ---: | ---: | ---: |
| Observation 1 | 0.339502249 | 138.671875 | 3 |
| Observation 2 | 0.339822885 | 138.671875 | 3 |
| Observation 3 | 0.339404005 | 138.656250 | 3 |
| Approved verification | 0.343546645 | 138.671875 | 3 |

There were exactly five allocated Jobs in this protocol: one qualification and
four API Jobs, totaling **20 NPU reservation seconds**, of which 12 belong to the
API usage ledger. The direct qualification remains a separate 8-second cost;
importing its classification report does not create usage. Prior failed-model
experiments retain their own costs and are not included in this protocol total.
All four API attempts had exactly one usage record, one MLflow run and matching
S3/API/MLflow result bytes. Five full classification reports were separately
imported into the project-scoped verification history.

The lookup used only the first three profile IDs, with mean 0.339576380 seconds.
Its mean uncertainty interval was [0.339196959, 0.339955800]; the subsequent
verification at 0.343546645 seconds was **outside** this interval. The interval
is not a calibrated future-Job guarantee. A single eligible configuration
demonstrates measured lookup and approval, not better placement or speedup.
The four Jobs repeat the same 100 images; they do not supply 400 independent
accuracy observations.

An initial approval request was rejected with HTTP 422 because the local driver
hashed the entire recommendation response, including its digest field. Using
the server-provided digest fixed the client request against the **same** saved
recommendation. The server guard was unchanged and no compute Job was replayed.

Preparation measurements are separate: parallel model/HEF downloads took
40.312/7.311 seconds, fixture preparation 9.217 seconds (including 5.266 seconds
of CPU reference inference), and qualification/platform image build-and-push
1.885/0.892 seconds. These are not additive end-to-end wall time and exclude
development, environment setup and operator/tool waiting.

A separate Hailo worker route uses the existing NPU LocalQueue and exact
qualified node; the two existing GPU project routes were preserved. Only the
idle worker configuration was reloaded; its image and all five deployment
container specs stayed unchanged. The existing 573 non-worker Pod identities,
GPU/NPU queue quota specs and ten Ready nodes were checked after execution.
The NPU queue, API jobs and delivery outbox were idle at the final check.
The deployed compatibility console was opened in a real browser through a
loopback-only read-only proxy to the live API. It displayed all five ResNet-50
records with the passed 80%/97% gates and the registered workload; no browser
console errors or warnings were observed. The proxy was stopped after the check.

The [public evidence bundle](evidence/hailo-resnet50-platform.json) retains raw
per-image timings, result signatures, scheduler timestamps, per-attempt usage,
approval chronology, storage read-back assertions and the client failure.
Private registry/storage locations and node names are omitted. Validate its
internal consistency without submitting any new work:

```sh
python examples/audit_hailo_platform.py \
  docs/evidence/hailo-resnet50-platform.json \
  docs/evidence/hailo-resnet50-inputs.json
```

This audit recalculates quality and allocation, rejects replacement/duplicate
runs and future-profile leakage, and verifies result/binding hashes. It cannot
independently repeat the recorded private S3/MLflow read-backs.

## Reproducing the integration

Prepare the exact public model/HEF and disjoint float32 inputs according to the
[plan](hailo-resnet50-plan.md). Use the existing hashed ARM64 runtime construction
in [the Hailo runtime guide](hailo-qualification.md#reproduction), preserving the
driver and device plugin. Archive a new qualification verdict before enabling
any platform workload. Never loop qualification until it passes.

The recorded [binding](evidence/hailo-resnet50-platform.json) fixes the precise
manifest digest. Reproduction changes timing fields in the prepared manifest,
so inspect matching model/input/reference hashes and explicitly qualify a new
binding instead of silently substituting another manifest into this runner.
This adapter deliberately supports only the published contract.

For this contract, bake `__init__.py`, `contracts.py`, `hailo_qualification.py`
and `hailo_benchmark.py` into `/opt/resource-advisor/resource_advisor`, the binding
into `/opt/platform-binding.json`, and fixtures into `/opt/fixture`. Retain the
Hailo runtime's Python paths while adding `/opt/resource-advisor`; use non-root
user 10001, writable `/tmp`, `HOME=/tmp` and `HAILORT_LOGGER_PATH=/tmp`. Pin the
resulting image digest. The RuntimeVariant command is:

```sh
python -m resource_advisor.hailo_benchmark \
  --binding /opt/platform-binding.json --fixture /opt/fixture \
  --hef /opt/fixture/model.hef
```

Register the qualified capability, variant and workload through operator/project
APIs, with a separate backend-cluster route selecting the existing NPU queue,
ARM64 node and `hailo.ai/h8` resource. The standard Kubernetes adapter supplies
the job/attempt/context identity environment. Observe three Jobs with stable
idempotency keys, validate their terminal results and delivery, request a measured
recommendation, approve using the response's `digest`, then submit one fixed Job
with that approval reference. This recipe needs a newly preregistered scope and
budget when used for another hardware/model/input contract.

## Edge log collection recovery

After the qualification finished, fetching its logs initially failed at the
edge fake-kubelet port. The four existing `cloud-iptables-manager` containers
were repeatedly OOM-killed with 50 MiB limits. They install the CloudStream DNAT
rules used by the [KubeEdge log/debug path](https://kubeedge.io/docs/advanced/debug/).

Only that DaemonSet's memory request/limit changed, from 25/50 MiB to 64/128 MiB.
CPU limits, images, CloudCore, node drivers and workload queue quotas were
preserved. The four replacement Pods became ready with zero restarts; observed
memory was 52–90 MiB. The **same completed qualification Pod's** logs were then
retrieved successfully. No qualification replay or replacement sample occurred.

Before applying such a repair, retain the original manifest, inspect node memory
headroom and confirm the actual last termination reason. Afterward verify
rollout, restart counts, resource use and retrieval from the original Pod. Keep
the rollback locally. These limits describe the measured lab repair, not a
universal production sizing recommendation.
