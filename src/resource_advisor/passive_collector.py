"""Observe an existing Linux process; never launch, signal, attach or modify it."""

import argparse
import ctypes as C
import hashlib
import json
import os
import time
from pathlib import Path

from .contracts import now, signature
from .passive import DeviceSample, NodeSample, PassiveImport, PassiveSample, ProcessSample
from .thermal import NvmlReader


class TargetChanged(ValueError):
    pass


def process_stat(text):
    # comm can include spaces/parentheses; fields after the final ')' start at state (3).
    fields = text.rsplit(")", 1)[1].split()
    return {
        "state": fields[0],
        "cpu_ticks": int(fields[11]) + int(fields[12]),
        "start_ticks": int(fields[19]),
        "rss_pages": max(0, int(fields[21])),
    }


class ProcReader:
    def __init__(self, pid, *, root=Path("/proc"), clock=time.perf_counter):
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise ValueError("positive target PID required")
        self.pid, self.root, self.clock = pid, root, clock
        self.path = root / str(pid)
        self.origin = clock()
        self.tick_rate = os.sysconf("SC_CLK_TCK")
        self.page_bytes = os.sysconf("SC_PAGE_SIZE")
        self.start_ticks = process_stat(self.path.joinpath("stat").read_text())["start_ticks"]
        self.identity = self._identity()

    def _identity(self):
        start = process_stat(self.path.joinpath("stat").read_text())["start_ticks"]
        if start != self.start_ticks:
            raise TargetChanged("process identity changed")
        namespace = self.path.joinpath("ns/pid").stat()
        return signature(
            {
                "boot": self.root.joinpath("sys/kernel/random/boot_id").read_text().strip(),
                "pid_namespace_device": namespace.st_dev,
                "pid_namespace_inode": namespace.st_ino,
                "pid": self.pid,
                "start_ticks": start,
            }
        )

    def optional(self, path):
        try:
            return path.read_text(), None
        except PermissionError:
            return None, "PERMISSION_DENIED"
        except (FileNotFoundError, ProcessLookupError):
            return None, "UNAVAILABLE"

    def node(self):
        cpu, _ = self.optional(self.root / "stat")
        memory, _ = self.optional(self.root / "meminfo")
        values = {}
        if cpu is not None:
            fields = next(line for line in cpu.splitlines() if line.startswith("cpu ")).split()[1:]
            ticks = [int(value) for value in fields]
            # guest/guest_nice are already included in user/nice; do not double-count.
            values.update(cpu_total_ticks=sum(ticks[:8]), cpu_idle_ticks=ticks[3] + ticks[4])
        if memory is not None:
            fields = dict(line.split(":", 1) for line in memory.splitlines())
            if "MemTotal" in fields and "MemAvailable" in fields:
                values.update(
                    memory_total_bytes=int(fields["MemTotal"].split()[0]) * 1024,
                    memory_available_bytes=int(fields["MemAvailable"].split()[0]) * 1024,
                )
        return NodeSample(**values)

    def read(self, device=None):
        started = self.clock() - self.origin
        if self._identity() != self.identity:
            raise TargetChanged("process namespace or boot identity changed")
        stat = process_stat(self.path.joinpath("stat").read_text())
        if stat["state"] in {"Z", "X"}:
            raise ProcessLookupError("target exited")
        io, error = self.optional(self.path / "io")
        values = {
            "cpu_seconds": stat["cpu_ticks"] / self.tick_rate,
            "rss_bytes": stat["rss_pages"] * self.page_bytes,
        }
        errors = {}
        counters = dict(line.split(":", 1) for line in io.splitlines()) if io else {}
        for name in ("read_bytes", "write_bytes"):
            if name in counters:
                values[name] = int(counters[name])
            else:
                errors[name] = error or "UNAVAILABLE"
        node = self.node()
        physical = device.read() if device is not None else None
        if self._identity() != self.identity:
            raise TargetChanged("target changed while reading")
        return PassiveSample(
            started_seconds=started,
            finished_seconds=self.clock() - self.origin,
            process=ProcessSample(**values, errors=errors),
            node=node,
            device=physical,
        )


class Utilization(C.Structure):
    _fields_ = [("gpu", C.c_uint), ("memory", C.c_uint)]


class Memory(C.Structure):
    _fields_ = [("total", C.c_ulonglong), ("free", C.c_ulonglong), ("used", C.c_ulonglong)]


class PassiveNvml:
    """An operator-specified physical UUID, never assumed to be device zero.

    These readings include other users of that physical GPU. No attribution to
    the observed process, no reservation/isolation or CUDA execution proof.
    """

    def __init__(self, uuid, *, reader=None):
        self.reader = reader or NvmlReader(uuid)

    def read(self):
        reader = self.reader
        thermal = reader.read()
        values = {
            "device_uuid_digest": reader.device_uuid_digest,
            "temperature_c": thermal.temperature_c,
            "power_w": thermal.power_w,
        }
        errors = {k: v for k, v in thermal.errors.items() if k in values}
        for field, call, struct, member in (
            ("utilization_percent", "nvmlDeviceGetUtilizationRates", Utilization, "gpu"),
            ("memory_used_bytes", "nvmlDeviceGetMemoryInfo", Memory, "used"),
        ):
            result = struct()
            code = reader._call(
                call, [C.c_void_p, C.POINTER(struct)], [reader.handle, C.byref(result)]
            )
            if code:
                errors[field] = code
            else:
                values[field] = getattr(result, member)
        return DeviceSample(**values, errors=errors)

    def close(self):
        self.reader.close()


def collector_digest():
    return signature(
        {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (
                Path(__file__),
                Path(__file__).with_name("passive.py"),
                Path(__file__).with_name("contracts.py"),
                Path(__file__).with_name("thermal.py"),
            )
        }
    )


def collect(
    reader,
    binding,
    *,
    duration,
    interval,
    device=None,
    device_reason="NOT_REQUESTED",
    sleep=time.sleep,
):
    if not 0 < duration <= 3600 or not 0.1 <= interval <= 60:
        raise ValueError("bounded duration/interval required")
    if duration / interval >= 600:
        raise ValueError("at most 600 observations per report")
    started = now()
    origin = reader.clock()
    reader.origin = origin
    samples, reason = [], "WINDOW_COMPLETE"
    while reader.clock() - origin < duration:
        try:
            samples.append(reader.read(device))
        except TargetChanged:
            reason = "TARGET_CHANGED"
            break
        except (FileNotFoundError, ProcessLookupError):
            reason = "PROCESS_EXITED"
            break
        except PermissionError:
            reason = "UNREADABLE"
            break
        remaining = duration - (reader.clock() - origin)
        if remaining > 0:
            sleep(min(interval, remaining))
    if not samples:
        raise ValueError("no stable target samples; no observation report created")
    return PassiveImport(
        **{key: value for key, value in binding.items() if key != "target"},
        collector_digest=collector_digest(),
        evidence_kind="hardware",
        target={**binding["target"], "process_identity_digest": reader.identity},
        started_at=started,
        finished_at=now(),
        requested_window_seconds=duration,
        interval_seconds=interval,
        stop_reason=reason,
        device_unavailable_reason=None if device is not None else device_reason,
        samples=tuple(samples),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=15)
    parser.add_argument("--interval", type=float, default=1)
    parser.add_argument(
        "--gpu-uuid", help="operator-verified physical UUID; readings include co-runners"
    )
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists; observations are immutable")
    binding = json.loads(args.binding.read_text())
    if set(binding) != {"ref", "project_ref", "target"}:
        parser.error("binding must contain only ref, project_ref and target")
    device, reason = None, "NOT_REQUESTED"
    try:
        if args.gpu_uuid:
            try:
                device = PassiveNvml(args.gpu_uuid)
            except (OSError, RuntimeError):
                reason = "NVML_UNAVAILABLE"
        reader = ProcReader(args.pid)
        report = collect(
            reader,
            binding,
            duration=args.duration,
            interval=args.interval,
            device=device,
            device_reason=reason,
        )
        with args.output.open("x") as stream:
            stream.write(json.dumps(report.model_dump(mode="json"), indent=2) + "\n")
        print(
            json.dumps(
                {
                    "ref": report.ref,
                    "samples": len(report.samples),
                    "stop_reason": report.stop_reason,
                }
            )
        )
    finally:
        if device is not None:
            device.close()


if __name__ == "__main__":
    main()
