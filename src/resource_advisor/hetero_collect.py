"""Read-only inventory/topology collectors for heterogeneous recommendation.

Parsers turn native read-only outputs into evidence records. Advertised resources,
labels and link speeds are inventory, never runtime qualification or measured
throughput. Missing values stay None; nothing here submits jobs or mutates state.
"""

import json
import re
import shutil
import subprocess

from .contracts import now

# Extended resources the lab exposes. Count semantics differ: shared slots and
# HAMi vGPU entries are logical, not physical devices.
RESOURCE_KEYS = {
    "nvidia.com/gpu": ("gpu", "physical_device"),
    "nvidia.com/gpu.shared": ("gpu", "virtual_slot"),
    "hami.io/vgpu": ("gpu", "virtual_slot"),
    "amd.com/gpu": ("gpu", "physical_device"),
    "hailo.ai/h8": ("npu", "physical_device"),
    "mobilint.com/npu": ("npu", "physical_device"),
    "npu.intel.com/accel": ("npu", "physical_device"),
    "deepx.ai/npu": ("npu", "physical_device"),
}
LABEL_HINTS = ("nvidia.com/gpu.product", "nvidia.com/gpu.memory", "nvidia.com/cuda.driver.major")


def _count(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def node_fingerprints(nodes):
    """``kubectl get nodes -o json`` → per-node fingerprints; labels are unverified hints."""
    result = []
    for node in nodes.get("items", []):
        meta, status = node["metadata"], node.get("status", {})
        info = status.get("nodeInfo", {})
        ready = next(
            (c["status"] for c in status.get("conditions", []) if c["type"] == "Ready"), None
        )
        alloc = status.get("allocatable", {})
        devices = [
            {
                "resource_key": key,
                "device_class": cls,
                "allocation_mode": mode,
                "allocatable": _count(alloc.get(key)),
                "count_unit": "logical_slot" if mode == "virtual_slot" else "physical_device",
            }
            for key, (cls, mode) in RESOURCE_KEYS.items()
            if key in alloc
        ]
        labels = meta.get("labels", {})
        result.append(
            {
                "node_ref": meta["name"],
                "ready": {"True": True, "False": False}.get(ready),
                "arch": info.get("architecture"),
                "os_image": info.get("osImage"),
                "kernel": info.get("kernelVersion"),
                "container_runtime": info.get("containerRuntimeVersion"),
                "kubelet": info.get("kubeletVersion"),
                "cpu": alloc.get("cpu"),
                "memory": alloc.get("memory"),
                "devices": devices,
                "label_hints": {k: labels[k] for k in LABEL_HINTS if k in labels},
                "runtime_verified": False,
                "driver_sdk_firmware": None,
            }
        )
    return result


def slurm_fingerprints(text):
    """``sinfo -N -h -o '%N|%G|%f|%c|%m|%T'`` → per-node records (GRES as configured)."""
    result = []
    for line in text.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 6:
            continue
        name, gres, features, cpus, memory, state = parts
        result.append(
            {
                "node_ref": name,
                "gres": [] if gres in {"", "(null)"} else gres.split(","),
                "features": [] if features in {"", "(null)"} else features.split(","),
                "cpu": _count(cpus),
                "memory_mib": _count(memory),
                "state": state,
                "runtime_verified": False,
            }
        )
    return result


def accelerator_pods(pods):
    """Non-terminal Pods holding a known accelerator key: external state, not reset."""
    from .inventory import pod_request

    return sorted(
        (
            {
                "namespace": pod["metadata"].get("namespace"),
                "name": pod["metadata"]["name"],
                "node": pod.get("spec", {}).get("nodeName"),
                "phase": pod.get("status", {}).get("phase"),
                "requests": {k: pod_request(pod, k) for k in RESOURCE_KEYS if pod_request(pod, k)},
            }
            for pod in pods.get("items", [])
            if pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
            and any(pod_request(pod, k) for k in RESOURCE_KEYS)
        ),
        key=lambda x: (x["node"] or "", x["namespace"] or "", x["name"]),
    )


LOCAL_TOOLS = {
    "nvidia-smi": [
        "nvidia-smi",
        "--query-gpu=name,driver_version,memory.total,uuid",
        "--format=csv,noheader",
    ],
    "hailortcli": ["hailortcli", "fw-control", "identify"],
    "rocm-smi": ["rocm-smi", "--showproductname"],
}


def local_tools(execute=None, which=shutil.which):
    """Supported local read-only tools; a missing tool is recorded, not guessed."""
    execute = execute or (
        lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    )
    result = {}
    for name, argv in LOCAL_TOOLS.items():
        if which(argv[0]) is None:
            result[name] = {"status": "TOOL_NOT_FOUND", "output": None}
            continue
        done = execute(argv)
        result[name] = {
            "status": "ok" if done.returncode == 0 else "FAILED",
            "output": done.stdout[-4096:] if done.returncode == 0 else None,
        }
    return result


# Existing probes. Each establishes only what its label says.
PROBE_HOOKS = {
    "gpu": {
        "command": ["python", "-m", "resource_advisor.cuda_probe"],
        "establishes": "CUDA runtime smoke on the allocated device; not AI training",
    },
    "npu": {
        "command": ["python", "-m", "resource_advisor.npu_probe"],
        "establishes": "NPU runtime/model smoke; NPU inference only, never training",
    },
    "cpu": {
        "command": ["python", "-m", "resource_advisor.cpu_benchmark"],
        "establishes": "CPU benchmark; not accelerator evidence",
    },
}


def link_command(url, *, max_bytes, max_seconds, limit_rate_bytes):
    """Bounded single-stream read of an explicitly permitted object."""
    if not re.fullmatch(r"https?://[A-Za-z0-9._:/%-]+", url):
        raise ValueError("explicit http(s) object URL required")
    if not (0 < max_bytes <= 2**30 and 0 < max_seconds <= 60 and 0 < limit_rate_bytes <= 2**30):
        raise ValueError("link probe caps: ≤1 GiB, ≤60 s, ≤1 GiB/s")
    return [
        "curl",
        "--silent",
        "--show-error",
        "--fail",
        "--output",
        "/dev/null",
        "--max-time",
        str(max_seconds),
        "--max-filesize",
        str(max_bytes),
        "--range",
        f"0-{max_bytes - 1}",
        "--limit-rate",
        str(limit_rate_bytes),
        "--write-out",
        "%{size_download} %{time_starttransfer} %{time_total}",
        url,
    ]


def link_measurement(output, *, source, target, evidence_ref, limit_rate_bytes, at=None):
    """curl write-out → TopologyLink body. A capped probe reports a lower bound."""
    size, first_byte, total = (float(x) for x in output.split())
    transfer = total - first_byte
    if size <= 0 or transfer <= 0:
        raise ValueError("no measurable transfer")
    bandwidth = size / transfer
    return {
        "link": {
            "source_endpoint_ref": source,
            "target_endpoint_ref": target,
            "bandwidth_bytes_per_second": bandwidth,
            "request_overhead_ms": first_byte * 1000,
            "measured_at": (at or now()).isoformat(),
            "evidence_ref": evidence_ref,
        },
        "provenance": {
            # At the cap, the result is a lower bound of the path, not its capacity.
            "rate_capped": bandwidth >= 0.95 * limit_rate_bytes,
            "bytes": size,
            "method": "curl single-stream bounded range read",
        },
    }


def dump(value):
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
