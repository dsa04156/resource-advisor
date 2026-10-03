"""Bounded CUDA activity evidence from an isolated PyTorch Kineto trace.

Kernel interval union is elapsed device activity, not SM utilization, achieved
FLOPs, or proof of arithmetic versus memory-bandwidth saturation. Profiler-on
durations never substitute for profiler-off performance measurements.
"""

import json
import math

PREFIX = "ra-e5-forward-"


def trace_evidence(trace):
    """Retain bounded parser input without host/process/path metadata."""
    events = trace.get("traceEvents")
    if not isinstance(events, list) or len(events) > 100000:
        raise ValueError("missing or oversized trace")
    selected = []
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("invalid trace event")
        name = event.get("name", "")
        if not (
            (isinstance(name, str) and name.startswith(PREFIX)) or event.get("cat") == "kernel"
        ):
            continue
        if not isinstance(name, str) or len(name) > 4096:
            raise ValueError("invalid trace name")
        _interval(event)
        row = {k: event[k] for k in ("name", "cat", "ph", "ts", "dur") if k in event}
        args = event.get("args", {})
        if not isinstance(args, dict):
            raise ValueError("invalid trace arguments")
        if "device" in args:
            row["args"] = {"device": args["device"]}
        selected.append(row)
    if not selected or len(selected) > 20000:
        raise ValueError("empty or oversized selected trace")
    origin = min(e["ts"] for e in selected)
    for event in selected:
        event["ts"] -= origin
    evidence = {"traceEvents": selected}
    if len(json.dumps(evidence, allow_nan=False).encode()) > 2 * 1024 * 1024:
        raise ValueError("selected trace exceeds two MiB")
    return evidence


def _interval(event):
    start, duration = event.get("ts"), event.get("dur")
    if (
        type(start) not in (int, float)
        or type(duration) not in (int, float)
        or not math.isfinite(start)
        or not math.isfinite(duration)
        or start < 0
        or duration <= 0
        or not math.isfinite(start + duration)
        or start + duration <= start
    ):
        raise ValueError("invalid trace timestamp/duration")
    return start, start + duration


def union_duration(intervals):
    """Microsecond interval union; concurrent streams cannot double-charge time."""
    merged = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(end, merged[-1][1])
    return math.fsum(end - start for start, end in merged)


def summarize_trace(trace, units):
    if type(units) is not int or not 3 <= units <= 16:
        raise ValueError("expected 3–16 measured blocks")
    events = trace.get("traceEvents")
    if not isinstance(events, list) or len(events) > 100000:
        raise ValueError("missing or oversized trace")
    markers = {}
    kernels = []
    ignored_gpu_annotations = 0
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("invalid trace event")
        name = event.get("name", "")
        if isinstance(name, str) and name.startswith(PREFIX):
            # Kineto emits a GPU-correlated annotation with the same name as
            # the CPU record_function range. It is neither another host range
            # nor a kernel; only actual kernel events contribute activity time.
            if event.get("cat") == "gpu_user_annotation":
                ignored_gpu_annotations += 1
                continue
            if name in markers or event.get("ph") != "X" or event.get("cat") != "user_annotation":
                raise ValueError("duplicate or incomplete forward marker")
            markers[name] = _interval(event)
        elif event.get("cat") == "kernel":
            if event.get("ph") != "X":
                raise ValueError("incomplete CUDA kernel event")
            args = event.get("args", {})
            if args.get("device") != 0 or type(args.get("device")) is not int:
                raise ValueError("unexpected CUDA device; isolated local device zero required")
            start, end = _interval(event)
            if not isinstance(name, str) or not name or len(name) > 4096:
                raise ValueError("invalid kernel name")
            kernels.append({"name": name, "start_us": start, "end_us": end})
    expected = {f"{PREFIX}{i:04d}" for i in range(units)}
    if set(markers) != expected or not kernels or len(kernels) > 10000:
        raise ValueError("missing block markers or CUDA activity; CPU fallback is not evidence")
    ordered = sorted(markers.values())
    if any(a[1] > b[0] for a, b in zip(ordered, ordered[1:], strict=False)):
        raise ValueError("overlapping serial forward markers")
    assigned = {name: [] for name in markers}
    for kernel in kernels:
        matches = [
            name
            for name, (start, end) in markers.items()
            if start <= kernel["start_us"] < kernel["end_us"] <= end
        ]
        if len(matches) != 1:
            raise ValueError("CUDA kernel outside an owned synchronized forward window")
        assigned[matches[0]].append(kernel)
    rows = []
    origin = min(a for a, _ in markers.values())
    for name in sorted(markers):
        start, end = markers[name]
        group = assigned[name]
        if not group:
            raise ValueError("measured block has no CUDA kernels")
        union = union_duration([(k["start_us"], k["end_us"]) for k in group])
        rows.append(
            {
                "sample_ref": name,
                "start_seconds": (start - origin) / 1e6,
                "forward_span_seconds": (end - start) / 1e6,
                "kernel_union_seconds": union / 1e6,
                "kernel_sum_seconds": math.fsum(k["end_us"] - k["start_us"] for k in group) / 1e6,
                "kernel_span_fraction": union / (end - start),
                "kernels": [
                    {
                        "name": k["name"],
                        "start_seconds": (k["start_us"] - origin) / 1e6,
                        "duration_seconds": (k["end_us"] - k["start_us"]) / 1e6,
                    }
                    for k in group
                ],
            }
        )
    span = math.fsum(r["forward_span_seconds"] for r in rows)
    active = math.fsum(r["kernel_union_seconds"] for r in rows)
    return {
        "provider": "pytorch-kineto-cuda-activity-v1",
        "profiler_enabled": True,
        "performance_profile_eligible": False,
        "kernel_count": len(kernels),
        "ignored_gpu_annotations": ignored_gpu_annotations,
        "samples": rows,
        "kernel_union_seconds": active,
        "forward_span_seconds": span,
        "kernel_span_fraction": active / span,
        "auto_apply": False,
        "resource_change": None,
        "semantics": "Union of traced CUDA kernel intervals within owned synchronized forward windows; not SM utilization",
    }
