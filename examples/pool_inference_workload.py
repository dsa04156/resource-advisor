"""Identical trained FP32 MLP inference on any CUDA device; stdlib and driver only.

Reuse cuda_probe's real driver/JIT/reservation path, with learned weights and
held-out digit images replacing its integer smoke kernel. No CPU inference fallback.
"""

import argparse
import base64
import ctypes as ct
import hashlib
import json
import math
import platform
import struct
import threading
import time
from pathlib import Path

PTX = b""".version 7.0
.target sm_50
.address_size 64
.visible .entry dense(
 .param .u64 X, .param .u64 W, .param .u64 B, .param .u64 Y,
 .param .u32 K, .param .u32 N, .param .u32 M, .param .u32 RELU) {
 .reg .pred %p<4>;
 .reg .u32 %r<16>;
 .reg .u64 %d<16>;
 .reg .f32 %f<8>;
 ld.param.u64 %d0,[X]; ld.param.u64 %d1,[W];
 ld.param.u64 %d2,[B]; ld.param.u64 %d3,[Y];
 ld.param.u32 %r0,[K]; ld.param.u32 %r1,[N];
 ld.param.u32 %r2,[M]; ld.param.u32 %r3,[RELU];
 mov.u32 %r4,%ctaid.x; mov.u32 %r5,%ntid.x; mov.u32 %r6,%tid.x;
 mad.lo.u32 %r7,%r4,%r5,%r6; mul.lo.u32 %r8,%r1,%r2;
 setp.ge.u32 %p0,%r7,%r8; @%p0 bra END;
 div.u32 %r9,%r7,%r1; rem.u32 %r10,%r7,%r1;
 mul.wide.u32 %d4,%r10,4; add.u64 %d5,%d2,%d4;
 ld.global.f32 %f0,[%d5]; mov.u32 %r11,0;
 LOOP:
 mad.lo.u32 %r12,%r9,%r0,%r11;
 mad.lo.u32 %r13,%r11,%r1,%r10;
 mul.wide.u32 %d6,%r12,4; mul.wide.u32 %d7,%r13,4;
 add.u64 %d8,%d0,%d6; add.u64 %d9,%d1,%d7;
 ld.global.f32 %f1,[%d8]; ld.global.f32 %f2,[%d9];
 fma.rn.f32 %f0,%f1,%f2,%f0;
 add.u32 %r11,%r11,1; setp.lt.u32 %p1,%r11,%r0; @%p1 bra LOOP;
 setp.ne.u32 %p2,%r3,0; @%p2 max.f32 %f0,%f0,0f00000000;
 mul.wide.u32 %d10,%r7,4; add.u64 %d11,%d3,%d10;
 st.global.f32 [%d11],%f0;
 END: ret;
}
"""


def unpack(record):
    raw = base64.b64decode(record["data"], validate=True)
    if hashlib.sha256(raw).hexdigest() != record["sha256"]:
        raise ValueError("fixture tensor digest mismatch")
    return raw


def sensor_reader(cuda, device):
    errors = []
    try:
        uuid = (ct.c_ubyte * 16)()
        if cuda.cuDeviceGetUuid(ct.byref(uuid), device) != 0:
            raise RuntimeError("CUDA UUID unavailable")
        value = bytes(uuid).hex()
        uuid_text = "GPU-" + "-".join(
            [value[:8], value[8:12], value[12:16], value[16:20], value[20:]]
        )
        nvml = ct.CDLL("libnvidia-ml.so.1")
        if nvml.nvmlInit_v2() != 0:
            raise RuntimeError("NVML init unavailable")
        handle = ct.c_void_p()
        if nvml.nvmlDeviceGetHandleByUUID(ct.c_char_p(uuid_text.encode()), ct.byref(handle)):
            raise RuntimeError("physical UUID handle unavailable")

        class Utilization(ct.Structure):
            _fields_ = [("gpu", ct.c_uint), ("memory", ct.c_uint)]

        def read():
            util = Utilization()
            result = nvml.nvmlDeviceGetUtilizationRates(handle, ct.byref(util))
            return dict(
                source="nvml-physical-uuid",
                utilization=util.gpu if result == 0 else None,
                error=None if result == 0 else "NVML_UTIL_" + str(result),
            )

        return read, errors
    except (OSError, RuntimeError) as exc:
        errors.append(str(exc))
    paths = list(Path("/sys/devices").glob("platform/*gpu*/load"))
    paths += list(Path("/sys/devices").glob("platform/*/*gpu*/load"))
    for path in paths:
        try:
            int(path.read_text())
        except (OSError, ValueError):
            continue

        def read(path=path):
            try:
                return dict(
                    source="jetson-gpu-load-permille",
                    utilization=int(path.read_text()) / 10,
                    error=None,
                )
            except (OSError, ValueError) as exc:
                return dict(source="jetson-gpu-load-permille", utilization=None, error=str(exc))

        return read, errors
    return lambda: dict(source="unavailable", utilization=None, error="NO_GPU_SENSOR"), errors


def run(fixture_path, rounds):
    if rounds < 1:
        raise ValueError("positive fixed work required")
    fixture_raw = fixture_path.read_bytes()
    fixture = json.loads(fixture_raw)
    cuda = ct.CDLL("libcuda.so.1")

    def call(name, *args):
        code = getattr(cuda, name)(*args)
        if code:
            raise RuntimeError(f"{name}: CUDA error {code}")

    call("cuInit", ct.c_uint(0))
    count, device, version = ct.c_int(), ct.c_int(), ct.c_int()
    call("cuDeviceGetCount", ct.byref(count))
    if count.value != 1:
        raise RuntimeError("exactly one visible GPU required")
    call("cuDeviceGet", ct.byref(device), ct.c_int(0))
    call("cuDriverGetVersion", ct.byref(version))
    name = ct.create_string_buffer(256)
    call("cuDeviceGetName", name, ct.c_int(256), device)
    ctx, module, fn = ct.c_void_p(), ct.c_void_p(), ct.c_void_p()
    allocations, samples = [], []
    read_sensor, sensor_errors = sensor_reader(cuda, device)
    sensor = []
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            before = time.time()
            sensor.append(dict(at=before, **read_sensor(), query_seconds=time.time() - before))
            stop.wait(0.2)

    thread = threading.Thread(target=sample, daemon=True)
    call("cuDevicePrimaryCtxRetain", ct.byref(ctx), device)
    call("cuCtxSetCurrent", ctx)

    def allocate(raw=None, size=None):
        pointer = ct.c_uint64()
        length = len(raw) if raw is not None else size
        call("cuMemAlloc_v2", ct.byref(pointer), ct.c_size_t(length))
        allocations.append(pointer)
        if raw is not None:
            call("cuMemcpyHtoD_v2", pointer, ct.c_char_p(raw), ct.c_size_t(length))
        return pointer

    try:
        call("cuModuleLoadData", ct.byref(module), ct.c_char_p(PTX))
        call("cuModuleGetFunction", ct.byref(fn), module, ct.c_char_p(b"dense"))
        current = allocate(unpack(fixture["input"]))
        batch = fixture["batch_size"]
        layers = []
        for index, layer in enumerate(fixture["layers"]):
            weights, bias = allocate(unpack(layer["weights"])), allocate(unpack(layer["bias"]))
            output = allocate(size=batch * layer["outputs"] * 4)
            values = [
                current,
                weights,
                bias,
                output,
                ct.c_uint(layer["inputs"]),
                ct.c_uint(layer["outputs"]),
                ct.c_uint(batch),
                ct.c_uint(index < len(fixture["layers"]) - 1),
            ]
            arguments = (ct.c_void_p * len(values))(*(ct.addressof(x) for x in values))
            layers.append((values, arguments, (batch * layer["outputs"] + 255) // 256))
            current = output

        def forward():
            for _, arguments, grid in layers:
                call(
                    "cuLaunchKernel",
                    fn,
                    ct.c_uint(grid),
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

        thread.start()
        for _ in range(3):
            forward()
        call("cuCtxSynchronize")
        started = time.time()
        for index in range(rounds):
            before = time.perf_counter()
            forward()
            call("cuCtxSynchronize")
            samples.append(time.perf_counter() - before)
            if (index + 1) % 256 == 0:
                print(
                    "POOL_PROGRESS "
                    + json.dumps(dict(rounds=index + 1, elapsed_seconds=sum(samples))),
                    flush=True,
                )
        finished = time.time()
        output = (ct.c_float * (batch * 10))()
        call("cuMemcpyDtoH_v2", output, current, ct.c_size_t(batch * 10 * 4))
        values = list(output)
        reference = struct.unpack("<" + "f" * len(values), unpack(fixture["reference"]))
        errors = [abs(a - b) for a, b in zip(values, reference, strict=True)]
        close = all(
            math.isfinite(a) and abs(a - b) <= 0.003 + 0.001 * abs(b)
            for a, b in zip(values, reference, strict=True)
        )
        predicted = [max(range(10), key=lambda k: values[i * 10 + k]) for i in range(batch)]
        expected = [max(range(10), key=lambda k: reference[i * 10 + k]) for i in range(batch)]
        agreement = sum(a == b for a, b in zip(predicted, expected, strict=True)) / batch
        if not close or agreement != 1:
            raise ValueError("trained model quality gate failed")
        return dict(
            measured=True,
            evidence_kind="hardware",
            outcome="COMPLETED",
            fixture_sha256=hashlib.sha256(fixture_raw).hexdigest(),
            kernel_sha256=hashlib.sha256(PTX).hexdigest(),
            accelerator_model=name.value.decode(),
            arch={"x86_64": "amd64", "aarch64": "arm64"}[platform.machine()],
            runtime_versions=dict(cuda_driver_api=str(version.value)),
            batch_size=batch,
            rounds=rounds,
            images=batch * rounds,
            compute_started_at=started,
            compute_finished_at=finished,
            elapsed_seconds=sum(samples),
            round_seconds=samples,
            quality=agreement,
            max_absolute_error=max(errors),
            accuracy=sum(a == b for a, b in zip(predicted, fixture["labels"], strict=True)) / batch,
            output_sha256=hashlib.sha256(bytes(output)).hexdigest(),
            tensor_allocation_bytes=len(unpack(fixture["input"]))
            + sum(
                len(unpack(x["weights"])) + len(unpack(x["bias"])) + batch * x["outputs"] * 4
                for x in fixture["layers"]
            ),
            sensor=sensor,
            sensor_errors=sensor_errors,
        )
    finally:
        stop.set()
        if thread.is_alive():
            thread.join(timeout=2)
        for pointer in reversed(allocations):
            call("cuMemFree_v2", pointer)
        if module.value:
            call("cuModuleUnload", module)
        call("cuDevicePrimaryCtxRelease_v2", device)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--rounds", type=int, required=True)
    args = parser.parse_args()
    print("POOL_INFERENCE_RESULT " + json.dumps(run(args.fixture, args.rounds)), flush=True)
