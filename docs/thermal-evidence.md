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

## Verification boundaries

`tests/test_thermal.py` covers serialization compatibility, context isolation,
missing/altered evidence rollback, project ownership, terminal immutability,
artifact/tracking delivery, thermal/error exclusions, no-next-job abstention,
bounded envelopes and NVML ABI fault doubles. Test sensor readings are synthetic.

An actual RTX 5080 discovery probe verified CUDA/NVML UUID resolution after
exposing the missing-prefix difference. Its NVML query returned driver `595.84`
and supported thermal-reason bits. That probe is not a measured-workload trace
or full thermal qualification. Full run/collector readback evidence is recorded
separately when verified; earlier GPU sampling reports remain unchanged.
