"""Bounded per-input NVML brackets. Observed trace eligibility is not thermal stability.

NVML calls are read-only. CUDA-visible UUID selects the device; physical index 0
is never assumed. Raw UUIDs are neither logged nor persisted by this module.
"""

import ctypes as C
import math
import re
import time
from typing import Literal

from pydantic import Field, model_validator

from .contracts import Contract, Digest, Ref, ThermalPolicy, signature

Sensor = Literal["temperature_c", "power_w", "sm_clock_mhz", "event_reasons", "supported_reasons"]
THERMAL_MASK = 0x60  # SW thermal (0x20) and HW thermal (0x40), NVML event definitions.
HW_SLOWDOWN_MASK = 0x8  # Ambiguous thermal/power/hardware cause, never silently ignored.


class Reading(Contract):
    started: float = Field(ge=0, le=86400)
    finished: float = Field(ge=0, le=86400)
    temperature_c: float | None = Field(default=None, ge=0, le=150)
    power_w: float | None = Field(default=None, ge=0, le=10000)
    sm_clock_mhz: int | None = Field(default=None, ge=0, le=100000)
    event_reasons: int | None = Field(default=None, ge=0, le=2**64 - 1)
    supported_reasons: int | None = Field(default=None, ge=0, le=2**64 - 1)
    errors: dict[Sensor, int] = Field(default_factory=dict, max_length=5)

    @model_validator(mode="after")
    def explicit_availability(self):
        if self.finished < self.started:
            raise ValueError("sensor query timestamps reversed")
        for name in (
            "temperature_c",
            "power_w",
            "sm_clock_mhz",
            "event_reasons",
            "supported_reasons",
        ):
            if (getattr(self, name) is None) != (name in self.errors):
                raise ValueError("each sensor needs a value or an explicit error")
        if any(code == 0 for code in self.errors.values()):
            raise ValueError("success is not a sensor error")
        return self


class Window(Contract):
    sample_ref: Ref
    before: Reading
    forward_started: float = Field(ge=0, le=86400)
    forward_finished: float = Field(ge=0, le=86400)
    after: Reading

    @model_validator(mode="after")
    def brackets(self):
        if (
            not self.before.finished
            <= self.forward_started
            < self.forward_finished
            <= self.after.started
        ):
            raise ValueError("sensor reads must bracket the complete timed forward")
        return self


class ThermalTrace(Contract):
    job_id: Ref
    attempt_id: Ref
    result_digest: Digest
    policy_digest: Digest
    driver_version: str = Field(min_length=1, max_length=64)
    device_uuid_digest: Digest
    method: Literal["nvml-brackets-v1"] = "nvml-brackets-v1"
    windows: tuple[Window, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def ordered(self):
        if len({w.sample_ref for w in self.windows}) != len(self.windows):
            raise ValueError("thermal windows must identify distinct inputs")
        if any(
            a.after.finished > b.before.started
            for a, b in zip(self.windows, self.windows[1:], strict=False)
        ):
            raise ValueError("thermal windows overlap or reverse")
        return self


def validate_trace(value, result, body):
    trace = ThermalTrace.model_validate(value)
    policy = ThermalPolicy.model_validate(body["variant"]["thermal_policy"])
    samples = body["sampling_binding"]["plan"]["samples"]
    if (
        result.outcome != "COMPLETED"
        or result.measurements is None
        or trace.job_id != result.job_id
        or trace.attempt_id != result.attempt_id
        or trace.result_digest != signature(result)
        or trace.policy_digest != signature(policy)
        or trace.driver_version != policy.driver_version
        or trace.device_uuid_digest != policy.device_uuid_digest
        or body["candidate"]["context"]["runtime_versions"].get("driver") != trace.driver_version
        or [w.sample_ref for w in trace.windows] != [s["ref"] for s in samples]
        or len(trace.windows) != result.measurements.work_units
        or len(trace.windows) != result.measurements.sample_count
        or not math.isclose(
            sum(w.forward_finished - w.forward_started for w in trace.windows),
            result.measurements.elapsed_seconds,
            rel_tol=1e-6,
            abs_tol=1e-9,
        )
    ):
        raise ValueError("thermal trace does not match the qualified measured result")
    return trace


def assess(trace, policy):
    trace = ThermalTrace.model_validate(trace)
    policy = ThermalPolicy.model_validate(policy)
    if (
        trace.policy_digest != signature(policy)
        or trace.driver_version != policy.driver_version
        or trace.device_uuid_digest != policy.device_uuid_digest
    ):
        raise ValueError("thermal trace policy/identity mismatch")
    readings = [r for w in trace.windows for r in (w.before, w.after)]
    reasons = set()
    if any(r.errors for r in readings):
        reasons.add("SENSOR_UNAVAILABLE")
    if any(
        r.temperature_c is not None and r.temperature_c > policy.maximum_temperature_c
        for r in readings
    ):
        reasons.add("TEMPERATURE_LIMIT_EXCEEDED")
    if any(r.event_reasons is not None and r.event_reasons & THERMAL_MASK for r in readings):
        reasons.add("THERMAL_SLOWDOWN_OBSERVED")
    if any(r.event_reasons is not None and r.event_reasons & HW_SLOWDOWN_MASK for r in readings):
        reasons.add("HARDWARE_SLOWDOWN_OBSERVED")
    if any(
        r.supported_reasons is None or r.supported_reasons & THERMAL_MASK != THERMAL_MASK
        for r in readings
    ):
        reasons.add("THERMAL_REASONS_UNSUPPORTED")
    durations = [r.finished - r.started for r in readings]
    gaps = [b.started - a.finished for a, b in zip(readings, readings[1:], strict=False)]
    if max(durations) > policy.maximum_query_seconds:
        reasons.add("SENSOR_QUERY_TOO_SLOW")
    if max(gaps, default=0) > policy.maximum_gap_seconds:
        reasons.add("OBSERVATION_GAP_TOO_LARGE")
    return {
        "status": "INELIGIBLE_TRACE" if reasons else "ELIGIBLE_TRACE",
        "reasons": sorted(reasons),
        "trace_digest": signature(trace),
        "maximum_observed_temperature_c": max(
            (r.temperature_c for r in readings if r.temperature_c is not None), default=None
        ),
        "sensor_query_seconds": sum(durations),
        "maximum_query_seconds": max(durations),
        "maximum_unobserved_gap_seconds": max(gaps, default=0),
        "reading_count": len(readings),
        "scope": "sampled points bracketing timed forwards; not continuous or long-duration thermal qualification",
    }


class NvmlReader:
    def __init__(self, cuda_uuid, *, library=None, clock=time.perf_counter):
        # Torch _CUuuid stringifies without NVML's GPU- prefix on CUDA 12.8.
        if not isinstance(cuda_uuid, str) or not re.fullmatch(
            r"(?:GPU-)?[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", cuda_uuid
        ):
            raise ValueError("physical CUDA GPU UUID required; MIG is not qualified")
        if not cuda_uuid.startswith("GPU-"):
            cuda_uuid = "GPU-" + cuda_uuid
        self.lib = library if library is not None else C.CDLL("libnvidia-ml.so.1")
        self.clock, self.origin = clock, clock()
        self.handle = C.c_void_p()
        self.closed = True
        self._required("nvmlInit_v2", [], [])
        self.closed = False
        try:
            self._required(
                "nvmlDeviceGetHandleByUUID",
                [C.c_char_p, C.POINTER(C.c_void_p)],
                [cuda_uuid.encode(), C.byref(self.handle)],
            )
            actual = self._string("nvmlDeviceGetUUID", [C.c_void_p], [self.handle])
            if actual.lower() != cuda_uuid.lower():
                raise RuntimeError("NVML device differs from CUDA device")
            self.device_uuid_digest = signature(actual.lower())
            self.driver_version = self._string("nvmlSystemGetDriverVersion", [], [])
        except BaseException:
            self.close()
            raise

    def _call(self, name, types, args):
        try:
            fn = getattr(self.lib, name)
        except AttributeError:
            return -1
        fn.argtypes, fn.restype = types, C.c_int
        return fn(*args)

    def _required(self, name, types, args):
        code = self._call(name, types, args)
        if code != 0:
            raise RuntimeError(f"NVML {name} failed with code {code}")

    def _string(self, name, types, args):
        buf = C.create_string_buffer(128)
        self._required(name, [*types, C.POINTER(C.c_char), C.c_uint], [*args, buf, C.c_uint(128)])
        return buf.value.decode("ascii")

    def elapsed(self):
        return self.clock() - self.origin

    def read(self):
        if self.closed:
            raise RuntimeError("NVML reader is closed")
        started = self.elapsed()
        values = {}
        errors = {}
        fields = (
            ("temperature_c", "nvmlDeviceGetTemperature", C.c_uint, [C.c_uint(0)], 1),
            ("power_w", "nvmlDeviceGetPowerUsage", C.c_uint, [], 1000),
            ("sm_clock_mhz", "nvmlDeviceGetClockInfo", C.c_uint, [C.c_uint(1)], 1),
            ("event_reasons", "nvmlDeviceGetCurrentClocksThrottleReasons", C.c_ulonglong, [], 1),
            (
                "supported_reasons",
                "nvmlDeviceGetSupportedClocksThrottleReasons",
                C.c_ulonglong,
                [],
                1,
            ),
        )
        for field, name, kind, extra, divisor in fields:
            result = kind()
            code = self._call(
                name,
                [C.c_void_p, *[C.c_uint for _ in extra], C.POINTER(kind)],
                [self.handle, *extra, C.byref(result)],
            )
            if code:
                values[field] = None
                errors[field] = code
            else:
                values[field] = result.value / divisor if divisor != 1 else result.value
        return Reading(started=started, finished=self.elapsed(), errors=errors, **values)

    def close(self):
        if not self.closed:
            self._call("nvmlShutdown", [], [])
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
