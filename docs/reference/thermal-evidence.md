# GPU thermal observations bound to an execution

`RuntimeVariant.thermal_policy` is an optional operator-qualified contract for
one physical NVIDIA GPU and 1–32 distinct sampled inputs. It pins the observed
driver version and a digest of the device UUID, plus preregistered temperature,
query-duration and sampling-gap limits. Changing the policy changes execution
context identity. Variants without this field keep their previous serialization
and context signatures; old measurements acquire no new telemetry claims.

The policy uses `provider=nvml-brackets-v1`, `driver_version`,
`device_uuid_digest`, `maximum_temperature_c`, `maximum_gap_seconds` and
`maximum_query_seconds`. The qualified runtime and candidate/capability runtime
versions must include the same `driver`. A thermal variant needs a registered
input sampling binding, one physical GPU, and no more than 32 measured inputs.
These bounded contracts fit the existing 64 KiB result transport.

## Device selection and raw observations

The reader selects the CUDA-visible physical UUID through
`nvmlDeviceGetHandleByUUID`, then verifies the returned NVML UUID. It never
assumes NVML index 0 identifies the allocated device. PyTorch's `_CUuuid` can
stringify without the `GPU-` prefix; the reader accepts a canonical UUID with
or without that prefix. MIG identifiers are unsupported. Only the UUID digest
is persisted, not the raw identifier.

Each timed forward has two NVML reads, immediately before and after the
forward-plus-CUDA-synchronization interval. Each read stores query start/end,
temperature in Celsius, power in watts, SM clock in MHz, current clock-event
reasons and the supported-reasons bitmask. NVML reports power in milliwatts;
the adapter converts that field to watts. Return codes, including unsupported
sensors, remain explicit errors with null values. No zeros are invented for
missing sensors. Driver/library/device-identity failures stop the run.

The adapter uses read-only NVML functions available in existing driver runtimes.
The older clock-throttle function names map to the documented clock-event reason
bits: software thermal slowdown `0x20`, hardware thermal slowdown `0x40`, and
ambiguous hardware slowdown `0x8`. GPU idle or software power caps are not
relabeled thermal events. See NVIDIA's [device queries](https://docs.nvidia.com/deploy/nvml-api/latest/api/group__nvmlDeviceQueries.html)
and [clock event reasons](https://docs.nvidia.com/deploy/nvml-api/latest/api/group__nvmlClocksEventReasons.html).

Input staging, sensor queries, CPU references and copies remain outside the
forward-only metric and inside Job wall cost. Sensor-query duration is reported
separately. Instrumentation can perturb execution and can greatly exceed the
duration of a tiny kernel; these observations are not free, continuous, or a
validated energy measurement. Initial NVML setup and warmup are outside the
reported query-duration sum but remain inside Job wall time.

## Collector and decision behavior

A `ThermalTrace` binds job/attempt/result digest, policy, driver/device identity
and an ordered window for every measured input. The collector validates window
ordering, exact input references/counts, sensor availability, nonoverlapping
timestamps and agreement between the sum of timed windows and the result's
elapsed time. A thermal-policy job cannot silently omit its trace. Invalid or
missing traces are retried under the bounded collection deadline; they do not
create accepted results, receipts or performance profiles. Terminal traces
cannot be replaced.

The server derives `ELIGIBLE_TRACE` or `INELIGIBLE_TRACE`. Missing sensors,
unsupported thermal-reason bits, excessive temperature, thermal/hardware
slowdown, slow queries or excessive gaps make a trace ineligible. The computation
can still be `SUCCEEDED` with passing numerical quality: thermal observation
eligibility is a separate fact. Its result and full trace remain available, but
it does not enter measured recommendation history. A pilot/confirmation with an
ineligible trace makes the durable study abstain before another submission.
Fidelity analysis rejects ineligible or altered configured traces.

`ELIGIBLE_TRACE` only means the recorded points satisfy that policy. It does not
establish absence of events between samples, sustained heat-load behavior,
stable candidate ranking or a qualified multi-fidelity axis. The existing MF-KG
execution rejection stays in place. All thermal policy values are laboratory
qualification criteria, not optimal scheduling thresholds or hardware limits.

`GET /api/v1/compute/jobs/{job_id}/thermal-trace` returns the project-owned trace
and assessment. Result artifacts include the trace, policy and assessment.
MLflow records status/reasons/digest tags and observed maximum temperature,
query time and maximum unobserved gap metrics; unknown values remain absent.
Both backend adapters transport the policy, and the native Slurm guard preserves
its environment variable. That code path alone is not Slurm hardware validation.

## Reproducing the bounded path

1. Use the existing qualified sampled CUDA runtime and an operator-approved
   sampling policy; the workload must bind 1–32 distinct inputs. Read the
   allocated CUDA device UUID through NVML and retain only its digest and
   the actual driver version. A historical driver's version is not evidence
   about the current runtime.
2. Register a new capability/context and immutable runtime variant containing
   the same `runtime_versions.driver`. Add `thermal_policy` with the observed
   driver/device digest and temperature, query and gap limits chosen before
   measured execution. The measured lab example uses 80°C, 50 ms per complete
   sensor read and 250 ms maximum unobserved gap. These are study criteria,
   not device safety limits.
3. Register the sampled workload and sampling binding. Use
   `python -m resource_advisor.sampled_gpu_benchmark --inputs <approved-directory>`
   as the qualified command and pilot command. The backend sets the identity,
   input plan and `RA_THERMAL_POLICY_JSON` from the approved job; users should
   not replace these variables inside notebook code.
4. Submit through the ordinary Job API or preregistered fidelity-calibration
   study. Keep each independent Job as one experimental unit. CPU/thread
   settings, measurement level, selection seed and confirmation reserve must
   be fixed in the registered study; observations within a Job are subsamples.
5. Read `/api/v1/compute/jobs/{job_id}/thermal-trace` and the result bundle.
   Compare stored trace digests and MLflow tags/metrics, then verify the
   retrieved S3/API/MLflow artifact bytes agree. An ineligible trace is retained
   with its cost and does not justify adjusting the threshold after seeing it.

Prior variants remain unchanged. Adding telemetry creates a new execution
context, so earlier uninstrumented timings are not silently pooled into it.

## Verification boundaries

`tests/test_thermal.py` covers serialization compatibility, context isolation,
missing/altered evidence rollback, project ownership, terminal immutability,
artifact/tracking delivery, thermal/error exclusions, no-next-job abstention,
bounded envelopes and NVML ABI fault doubles. Test sensor readings are synthetic.

An actual RTX 5080 discovery probe verified CUDA/NVML UUID resolution after
exposing the missing-prefix difference. Its NVML query returned driver `595.84`
and supported thermal-reason bits. The failed prefix probe, format diagnostic
and successful probe each reserved two GPU-seconds; all three remain accounted.

## Actual GPU execution and delivery

The [raw report](../evidence/thermal-gpu-calibration.json) and
[per-Job CSV](../evidence/thermal-gpu-calibration.csv) record a new, separately
registered trial on application source `813dc6216eed5b3bd79581ddace670134dd5deea`.
API, inventory and worker source files were checked inside their running Pods
against source-tree digest
`24a2b42a07a083a9c839b86aef9b66950ea9d2ddf150c125fc163841b8d0970c`.
The existing Python/PyTorch/CUDA dependencies and host driver were retained.

Four fresh F0 Jobs passed before workload registration, costing nine GPU
reservation seconds. The subsequent durable study completed 12 calibration
probes and six independent target confirmations: 18 unique successful Jobs,
40 GPU reservation seconds and 383.234 seconds of study wall time. Every Job
passed numerical quality and trace eligibility. Only the six confirmations
entered measured profile history. The study used two host CPU settings and
four/eight distinct generated matrix inputs; it was a bounded integration trial.

The 240 recorded sensor reads observed 32–33°C, 4.201–6.616 W and 180–690 MHz;
recorded clock-event masks were zero. These are NVML point values around tiny
kernels, not average active-kernel power or proof of no throttling between reads.
The largest unobserved gap was 0.525 ms and the largest complete sensor read
was 2.338 ms. Total sensor-query time was 41.066 ms versus 3.188 ms of timed
forward execution: about 12.9 times as much. Instrumentation overhead is material
for this workload and remains inside Job wall cost.

Backend-emitted result/trace, collector DB records and the project-owned trace
API agreed for every Job. All 18 S3/API/MLflow result bundles matched byte for
byte; MLflow temperature/query/gap metrics and trace tags matched the stored
assessment. Each attempt had one finished MLflow run. Kueue owner UIDs,
admission, physical GPU reservation and successful Pods were verified. The
final queue and delivery outbox were empty; all ten nodes were Ready without
pressure conditions at capture time.

The independent confirmation selected baseline `cpu1`; its interval overlapped
`cpu2`. The stored API status is `CONFIRMED_RECOMMENDATION`, which here confirms
the selected baseline's executable result, not a statistically established
improvement. The earlier probe prediction was `cpu2`; neither historical
uninstrumented results nor these confirmations entered the MF evidence input.
The evidence endpoint verified thermal brackets for the 12 probes but retained
`PAIRED_RANK_AND_THERMAL_QUALIFICATION_REQUIRED` and `execution_authorized=false`.
A correctly shaped live MF-KG request returned HTTP 422 with
`MF_KG_DISABLED: no qualified paired-fidelity group`.

This trial does not establish sustained thermal behavior, real-dataset fidelity,
calibrated intervals, candidate ranking stability or strategy superiority.
Earlier reports retain their original missing-thermal qualification status.
