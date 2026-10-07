"""Small real CUDA kernel for fleet bring-up; no framework or CPU fallback.

Measures synchronized kernel calls, not AI model performance. Device memory is
the known output allocation only, not context/driver peak memory. Shared slots
are supported without claiming memory isolation or a physical-device reservation.
"""

import argparse
import ctypes as ct
import hashlib
import json
import os
import platform
import statistics
import sys
import time

PTX = b""".version 7.0
.target sm_50
.address_size 64
.visible .entry squares(.param .u64 output) {
 .reg .u32 %r<5>;
 .reg .u64 %p<4>;
 ld.param.u64 %p0, [output];
 mov.u32 %r0, %ctaid.x;
 mov.u32 %r1, %ntid.x;
 mov.u32 %r2, %tid.x;
 mad.lo.u32 %r3, %r0, %r1, %r2;
 mul.lo.u32 %r4, %r3, %r3;
 mul.wide.u32 %p1, %r3, 4;
 add.u64 %p2, %p0, %p1;
 st.global.u32 [%p2], %r4;
 ret;
}
"""
SIZE, ROUNDS = 4096, 10
BOUNDARY = "cuda-squares-4096-sync-kernel-v1-output-buffer-memory"


def digest(value):
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
    )


def measure(context=None, sustained_seconds=0):
    if sustained_seconds not in {0, 30, 60, 90}:
        raise ValueError("Sustained CUDA runs must be 30, 60 or 90 seconds")
    cuda = ct.CDLL("libcuda.so.1")

    def call(name, *args):
        code = getattr(cuda, name)(*args)
        if code:
            raise RuntimeError(f"{name} failed: CUDA error {code}")

    call("cuInit", ct.c_uint(0))
    count, device, version = ct.c_int(), ct.c_int(), ct.c_int()
    call("cuDeviceGetCount", ct.byref(count))
    if count.value != 1:
        raise RuntimeError("Exactly one visible CUDA device is required")
    call("cuDeviceGet", ct.byref(device), ct.c_int(0))
    name = ct.create_string_buffer(256)
    call("cuDeviceGetName", name, ct.c_int(256), device)
    call("cuDriverGetVersion", ct.byref(version))
    info = {
        "arch": {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine()),
        "accelerator_model": name.value.decode(),
        "runtime_versions": {"cuda_driver_api": str(version.value)},
    }
    if context is not None:
        if any(context[k] != info[k] for k in info):
            raise RuntimeError("Device/runtime changed since qualification")
        if context["resources"]["accelerator_count"] != 1 or context["allocation_mode"] not in {
            "physical_device",
            "virtual_slot",
        }:
            raise RuntimeError("One physical device or one logical slot must be requested")
    cuda_context, module, function, pointer = (
        ct.c_void_p(),
        ct.c_void_p(),
        ct.c_void_p(),
        ct.c_uint64(),
    )
    call("cuDevicePrimaryCtxRetain", ct.byref(cuda_context), device)
    try:
        call("cuCtxSetCurrent", cuda_context)
        call("cuModuleLoadData", ct.byref(module), ct.c_char_p(PTX))
        call("cuModuleGetFunction", ct.byref(function), module, ct.c_char_p(b"squares"))
        call("cuMemAlloc_v2", ct.byref(pointer), ct.c_size_t(SIZE * 4))
        arguments = (ct.c_void_p * 1)(ct.addressof(pointer))
        timings = []
        launches = 0
        for iteration in range(ROUNDS + 2):
            start = time.perf_counter()
            window = sustained_seconds / ROUNDS if iteration >= 2 else 0
            while True:
                call(
                    "cuLaunchKernel",
                    function,
                    ct.c_uint(SIZE // 256),
                    ct.c_uint(1),
                    ct.c_uint(1),
                    ct.c_uint(256),
                    ct.c_uint(1),
                    ct.c_uint(1),
                    ct.c_uint(0),
                    ct.c_void_p(),
                    arguments,
                    ct.c_void_p(),
                )
                call("cuCtxSynchronize")
                launches += 1
                if time.perf_counter() - start >= window:
                    break
            if iteration >= 2:
                timings.append(time.perf_counter() - start)
                if sustained_seconds:
                    print(
                        "CUDA_PROGRESS "
                        + json.dumps(
                            {
                                "completed_windows": iteration - 1,
                                "total_windows": ROUNDS,
                                "elapsed_compute_seconds": sum(timings),
                                "kernel_launches": launches,
                            }
                        ),
                        flush=True,
                    )
        output = (ct.c_uint32 * SIZE)()
        call("cuMemcpyDtoH_v2", output, pointer, ct.c_size_t(SIZE * 4))
        correct = sum(value == i * i for i, value in enumerate(output))
        if correct != SIZE:
            raise RuntimeError(f"CUDA result mismatch: {correct}/{SIZE}")
        return {
            **info,
            "quality_value": 1.0,
            "elements_checked": SIZE,
            "kernel_sha256": hashlib.sha256(PTX).hexdigest(),
            "measurement_boundary": (
                BOUNDARY
                if not sustained_seconds
                else BOUNDARY + f"-timed-{sustained_seconds}s-windows"
            ),
            "sustained_seconds": sustained_seconds,
            "kernel_launches": launches,
            "samples_seconds": timings,
            "output_allocation_bytes": SIZE * 4,
        }
    finally:
        if pointer.value:
            call("cuMemFree_v2", pointer)
        if module.value:
            call("cuModuleUnload", module)
        call("cuDevicePrimaryCtxRelease_v2", device)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qualify", action="store_true")
    parser.add_argument("--sustain-seconds", type=int, choices=(30, 60, 90), default=0)
    args = parser.parse_args()
    qualification = args.qualify
    context = None if qualification else json.loads(os.environ["RA_CONTEXT_JSON"])
    if not qualification and (
        json.loads(os.environ["RA_INPUT_SHAPE"]) != [SIZE]
        or int(os.environ["RA_WORK_UNITS"]) != ROUNDS
        or os.environ["RA_PRECISION"] != "int32"
    ):
        raise ValueError("Only the fixed 4096-element, 10-call int32 contract is supported")
    if (
        context is not None
        and context.get("parameters", {}).get("sustained_seconds", 0) != args.sustain_seconds
    ):
        raise ValueError("Submitted duration differs from qualified context")
    report = measure(context, args.sustain_seconds)
    print("CUDA_PROBE_REPORT " + json.dumps(report), flush=True)
    if qualification:
        return
    timings = report["samples_seconds"]
    ordered = sorted(timings)
    measurements = {
        "elapsed_seconds": sum(timings),
        "peak_memory_mib": SIZE * 4 / 2**20,
        "quality_value": 1.0,
        "sample_count": ROUNDS,
        "work_units": ROUNDS,
        "latency_p50_ms": statistics.median(timings) * 1000,
        "latency_p95_ms": ordered[int((ROUNDS - 1) * 0.95)] * 1000,
        "latency_p99_ms": ordered[int((ROUNDS - 1) * 0.99)] * 1000,
        "throughput": ROUNDS / sum(timings),
        "gpu_utilization": None,
        "power_watts": None,
        "temperature_celsius": None,
    }
    result = {
        "schema_version": "v1",
        "job_id": os.environ["RA_JOB_ID"],
        "attempt_id": os.environ["RA_ATTEMPT_ID"],
        "epoch": int(os.environ["RA_EPOCH"]),
        "workload_signature": os.environ["RA_WORKLOAD_SIGNATURE"],
        "context_signature": os.environ["RA_CONTEXT_SIGNATURE"],
        "outcome": "COMPLETED",
        "measured": True,
        "evidence_kind": "hardware",
        "measurements": measurements,
        "error_code": None,
    }
    print("RESOURCE_ADVISOR_RESULT " + json.dumps({"result": result, "digest": digest(result)}))


if __name__ == "__main__":
    main()
