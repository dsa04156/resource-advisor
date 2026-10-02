"""F0 CUDA kernel verification using the driver only; no benchmark claim.

No Torch/toolkit dependency. Run under a real single-GPU scheduler reservation.
The driver JIT-compiles the small PTX kernel; successful enumeration alone is not
accepted. This does not verify container/device isolation or model correctness.
"""

import ctypes as c
import json

PTX = b"""
.version 6.4
.target sm_53
.address_size 64
.visible .entry write_sequence(.param .u64 output, .param .u32 count) {
    .reg .pred %p;
    .reg .b32 %r<4>;
    .reg .b64 %rd<3>;
    ld.param.u64 %rd1, [output];
    ld.param.u32 %r1, [count];
    mov.u32 %r2, %tid.x;
    mov.u32 %r3, %ctaid.x;
    mad.lo.u32 %r2, %r3, 256, %r2;
    setp.ge.u32 %p, %r2, %r1;
    @%p bra END;
    mul.wide.u32 %rd2, %r2, 4;
    add.s64 %rd2, %rd1, %rd2;
    add.u32 %r2, %r2, 1;
    st.global.u32 [%rd2], %r2;
END:
    ret;
}
"""


def main():
    cuda = c.CDLL("libcuda.so.1")

    def call(name, types, *args):
        fn = getattr(cuda, name)
        fn.argtypes, fn.restype = types, c.c_int
        result = fn(*args)
        if result:
            raise RuntimeError(f"{name} failed with CUDA error {result}")

    ptr = c.c_void_p
    iptr, vptr = c.POINTER(c.c_int), c.POINTER(ptr)
    call("cuInit", [c.c_uint], 0)
    count, version, device = c.c_int(), c.c_int(), c.c_int()
    call("cuDeviceGetCount", [iptr], c.byref(count))
    if count.value != 1:
        raise RuntimeError("F0 requires exactly one visible physical CUDA device")
    call("cuDriverGetVersion", [iptr], c.byref(version))
    call("cuDeviceGet", [iptr, c.c_int], c.byref(device), 0)
    context, module, function = ptr(), ptr(), ptr()
    allocation = c.c_uint64()
    n = c.c_uint(4096)
    call("cuCtxCreate_v2", [vptr, c.c_uint, c.c_int], c.byref(context), 0, device)
    try:
        ptx = c.create_string_buffer(PTX)
        call("cuModuleLoadData", [vptr, ptr], c.byref(module), ptx)
        call(
            "cuModuleGetFunction",
            [vptr, ptr, c.c_char_p],
            c.byref(function),
            module,
            b"write_sequence",
        )
        call("cuMemAlloc_v2", [c.POINTER(c.c_uint64), c.c_size_t], c.byref(allocation), n.value * 4)
        params = (ptr * 2)(c.cast(c.byref(allocation), ptr), c.cast(c.byref(n), ptr))
        call(
            "cuLaunchKernel",
            [ptr, *([c.c_uint] * 7), ptr, vptr, vptr],
            function,
            16,
            1,
            1,
            256,
            1,
            1,
            0,
            None,
            params,
            None,
        )
        call("cuCtxSynchronize", [])
        host = (c.c_uint32 * n.value)()
        call("cuMemcpyDtoH_v2", [ptr, c.c_uint64, c.c_size_t], host, allocation, n.value * 4)
        if list(host) != list(range(1, n.value + 1)):
            raise RuntimeError("GPU kernel result differs from reference")
        print(
            json.dumps(
                {
                    "phase": "F0",
                    "performance_measurement": False,
                    "cuda_devices": count.value,
                    "driver_api": version.value,
                    "verified_elements": n.value,
                    "result": "PASS",
                }
            )
        )
    finally:
        if allocation.value:
            call("cuMemFree_v2", [c.c_uint64], allocation)
        if module.value:
            call("cuModuleUnload", [ptr], module)
        call("cuCtxDestroy_v2", [ptr], context)


if __name__ == "__main__":
    main()
