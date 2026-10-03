"""Read-only container cgroup counters, bound to an execution result.

Counters describe the enclosing collection window and all processes in that
container, not only useful model compute. PSI, throttling and block-I/O bytes
are distinct signals; none is GPU utilization or proof of a slowdown's cause.
"""

import math
import os
import platform
import time
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import Contract, Digest, Ref, signature

PROVIDER = "linux-cgroup-v2-brackets-v1"
Counter = Annotated[int, Field(strict=True, ge=0)]


class LoadReading(Contract):
    started: float = Field(ge=0)
    finished: float = Field(ge=0)
    cpu_usage_usec: Counter
    cpu_periods: Counter
    cpu_throttled_periods: Counter
    cpu_throttled_usec: Counter
    cpu_pressure_some_usec: Counter | None = None
    io_pressure_some_usec: Counter | None = None
    io_read_bytes: Counter | None = None
    io_write_bytes: Counter | None = None
    memory_current_bytes: Counter
    observed_process_count: Counter
    cpu_quota_usec: Counter | None
    cpu_period_usec: int = Field(strict=True, gt=0)
    memory_limit_bytes: Counter | None

    @model_validator(mode="after")
    def ordered_read(self):
        if self.finished < self.started:
            raise ValueError("load read timestamps reversed")
        if self.cpu_throttled_periods > self.cpu_periods:
            raise ValueError("throttled periods exceed total periods")
        return self


class LoadTrace(Contract):
    schema_version: Literal["v1"] = "v1"
    provider: Literal["linux-cgroup-v2-brackets-v1"] = PROVIDER
    scope: Literal["container-cgroup-inclusive-run-window"] = (
        "container-cgroup-inclusive-run-window"
    )
    job_id: Ref
    attempt_id: Ref
    result_digest: Digest
    context_signature: Digest
    cgroup_identity_digest: Digest
    kernel_release: str = Field(min_length=1, max_length=128)
    before: LoadReading
    after: LoadReading


COUNTERS = (
    "cpu_usage_usec",
    "cpu_periods",
    "cpu_throttled_periods",
    "cpu_throttled_usec",
    "cpu_pressure_some_usec",
    "io_pressure_some_usec",
    "io_read_bytes",
    "io_write_bytes",
)


def validate_trace(value, result, body):
    trace = LoadTrace.model_validate(value)
    before, after = trace.before, trace.after
    if (
        body["variant"].get("load_context_policy") != PROVIDER
        or trace.job_id != result.job_id
        or trace.attempt_id != result.attempt_id
        or trace.result_digest != signature(result)
        or trace.context_signature != result.context_signature
        or result.measurements is None
        or after.started <= before.finished
        or after.finished - before.started < result.measurements.elapsed_seconds
    ):
        raise ValueError("load trace identity or enclosing window mismatch")
    requested = body["candidate"]["context"]["resources"]
    for name in ("cpu_quota_usec", "cpu_period_usec", "memory_limit_bytes"):
        if getattr(before, name) != getattr(after, name):
            raise ValueError("container limits changed during collection")
    for reading in (before, after):
        if (
            reading.cpu_quota_usec is None
            or not math.isclose(
                reading.cpu_quota_usec / reading.cpu_period_usec,
                requested["host_cpu"],
                rel_tol=1e-6,
            )
            or reading.memory_limit_bytes != requested["host_memory_mib"] * 1024**2
        ):
            raise ValueError("container limits differ from declared resources")
    for name in COUNTERS:
        a, b = getattr(before, name), getattr(after, name)
        if (a is None) != (b is None) or (a is not None and b < a):
            raise ValueError("load counter disappeared or reset")
    return trace


def summarize(trace):
    trace = LoadTrace.model_validate(trace)
    a, b = trace.before, trace.after
    values = {}
    for name in COUNTERS:
        before, after = getattr(a, name), getattr(b, name)
        values[name] = after - before if before is not None and after is not None else None
    periods, throttled = values["cpu_periods"], values["cpu_throttled_periods"]
    return {
        "provider": PROVIDER,
        "trace_digest": signature(trace),
        "scope": trace.scope,
        "window_seconds": b.finished - a.started,
        "read_seconds": (a.finished - a.started) + (b.finished - b.started),
        "cpu_usage_seconds": values["cpu_usage_usec"] / 1e6,
        "cpu_throttled_seconds": values["cpu_throttled_usec"] / 1e6,
        "cpu_throttled_period_fraction": throttled / periods if periods else None,
        "cpu_pressure_some_seconds": values["cpu_pressure_some_usec"] / 1e6
        if values["cpu_pressure_some_usec"] is not None
        else None,
        "io_pressure_some_seconds": values["io_pressure_some_usec"] / 1e6
        if values["io_pressure_some_usec"] is not None
        else None,
        "io_read_bytes": values["io_read_bytes"],
        "io_write_bytes": values["io_write_bytes"],
        "before_processes": a.observed_process_count,
        "after_processes": b.observed_process_count,
        "missing_counters": [name for name, value in values.items() if value is None],
        "semantics": "Container counters including setup/warmup/co-runners; not model-only CPU,"
        " host-wide load, task count, disk saturation, GPU utilization or causal diagnosis.",
    }


def flat_counters(text):
    values = {}
    for line in text.splitlines():
        key, value = line.split()
        if key in values or not value.isdecimal():
            raise ValueError("invalid cgroup counter")
        values[key] = int(value)
    return values


def pressure_total(text):
    rows = [line.split()[1:] for line in text.splitlines() if line.startswith("some ")]
    if len(rows) != 1:
        raise ValueError("one some-pressure total required")
    fields = dict(token.split("=", 1) for token in rows[0])
    value = fields["total"]
    if not value.isdecimal():
        raise ValueError("invalid pressure total")
    return int(value)


def io_bytes(text):
    totals = {"rbytes": 0, "wbytes": 0}
    devices = set()
    for line in text.splitlines():
        device, *fields = line.split()
        if device in devices:
            raise ValueError("duplicate block device counter")
        devices.add(device)
        fields = dict(token.split("=", 1) for token in fields)
        for name in totals:
            value = fields[name]
            if not value.isdecimal():
                raise ValueError("invalid I/O byte counter")
            totals[name] += int(value)
    return totals["rbytes"], totals["wbytes"]


class CgroupReader:
    """Qualified private cgroup-v2 namespace only; no host mounts or writes."""

    def __init__(
        self, root=Path("/sys/fs/cgroup"), proc=Path("/proc/self"), clock=time.perf_counter
    ):
        self.root, self.proc, self.clock = root, proc, clock
        self.origin = clock()
        self._check_namespace()
        self.identity = self._identity()

    def _check_namespace(self):
        if self.proc.joinpath("cgroup").read_text().strip() != "0::/":
            raise ValueError("private cgroup-v2 namespace required")
        mounts = self.proc.joinpath("mountinfo").read_text().splitlines()
        if not any(
            len(parts := line.split()) > 9 and parts[4] == str(self.root) and " - cgroup2 " in line
            for line in mounts
        ):
            raise ValueError("cgroup-v2 mount not verified")

    def _identity(self):
        stat = self.root.stat()
        return signature({"device": stat.st_dev, "inode": stat.st_ino})

    def optional(self, name):
        try:
            return self.root.joinpath(name).read_text()
        except (FileNotFoundError, PermissionError):
            return None

    def read(self):
        start = self.clock() - self.origin
        self._check_namespace()
        if self._identity() != self.identity:
            raise ValueError("cgroup identity changed")
        cpu = flat_counters(self.root.joinpath("cpu.stat").read_text())
        quota, period = self.root.joinpath("cpu.max").read_text().split()
        memory_limit = self.root.joinpath("memory.max").read_text().strip()
        cpu_pressure, io_pressure = self.optional("cpu.pressure"), self.optional("io.pressure")
        io = self.optional("io.stat")
        read_bytes, write_bytes = io_bytes(io) if io is not None else (None, None)
        processes = {
            int(line) for line in self.root.joinpath("cgroup.procs").read_text().splitlines()
        }
        if os.getpid() not in processes:
            raise ValueError("reader process is not in the observed cgroup")
        memory_current = int(self.root.joinpath("memory.current").read_text())
        cpu_psi = pressure_total(cpu_pressure) if cpu_pressure is not None else None
        io_psi = pressure_total(io_pressure) if io_pressure is not None else None
        return LoadReading(
            started=start,
            finished=self.clock() - self.origin,
            cpu_usage_usec=cpu["usage_usec"],
            cpu_periods=cpu["nr_periods"],
            cpu_throttled_periods=cpu["nr_throttled"],
            cpu_throttled_usec=cpu["throttled_usec"],
            cpu_pressure_some_usec=cpu_psi,
            io_pressure_some_usec=io_psi,
            io_read_bytes=read_bytes,
            io_write_bytes=write_bytes,
            memory_current_bytes=memory_current,
            observed_process_count=len({p for p in processes if p > 0}),
            cpu_quota_usec=None if quota == "max" else int(quota),
            cpu_period_usec=int(period),
            memory_limit_bytes=None if memory_limit == "max" else int(memory_limit),
        )

    def trace(self, before, after, result):
        return LoadTrace(
            job_id=result.job_id,
            attempt_id=result.attempt_id,
            result_digest=signature(result),
            context_signature=result.context_signature,
            cgroup_identity_digest=self.identity,
            kernel_release=platform.release(),
            before=before,
            after=after,
        )
