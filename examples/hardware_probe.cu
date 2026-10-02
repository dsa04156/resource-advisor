// F0 CUDA execution qualification. This is NOT a performance measurement.
#include <cuda_runtime.h>
#include <cstdio>
#include <vector>

#define CUDA_OK(call) do { const cudaError_t status = (call); if (status != cudaSuccess) { \
    std::fprintf(stderr, "CUDA verification failed: %s\n", cudaGetErrorString(status)); return 1; \
} } while (0)

__global__ void add(const float* a, const float* b, float* out, int n) {
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) out[i] = a[i] + b[i];
}

int main() {
    int count = 0, driver = 0, runtime = 0;
    CUDA_OK(cudaGetDeviceCount(&count));
    if (count != 1) {
        std::fprintf(stderr, "Expected exactly one allocated visible CUDA device; got %d\n", count);
        return 2;
    }
    CUDA_OK(cudaSetDevice(0));
    cudaDeviceProp device{};
    CUDA_OK(cudaGetDeviceProperties(&device, 0));
    CUDA_OK(cudaDriverGetVersion(&driver));
    CUDA_OK(cudaRuntimeGetVersion(&runtime));
    constexpr int n = 4096;
    std::vector<float> a(n), b(n), output(n);
    for (int i = 0; i < n; ++i) { a[i] = static_cast<float>(i); b[i] = 2.0f; }
    float *da = nullptr, *db = nullptr, *dc = nullptr;
    CUDA_OK(cudaMalloc(&da, n * sizeof(float)));
    CUDA_OK(cudaMalloc(&db, n * sizeof(float)));
    CUDA_OK(cudaMalloc(&dc, n * sizeof(float)));
    CUDA_OK(cudaMemcpy(da, a.data(), n * sizeof(float), cudaMemcpyHostToDevice));
    CUDA_OK(cudaMemcpy(db, b.data(), n * sizeof(float), cudaMemcpyHostToDevice));
    add<<<16, 256>>>(da, db, dc, n);
    CUDA_OK(cudaGetLastError());
    CUDA_OK(cudaDeviceSynchronize());
    CUDA_OK(cudaMemcpy(output.data(), dc, n * sizeof(float), cudaMemcpyDeviceToHost));
    for (int i = 0; i < n; ++i) {
        if (output[i] != a[i] + b[i]) {
            std::fprintf(stderr, "GPU numerical verification failed at element %d\n", i);
            return 3;
        }
    }
    CUDA_OK(cudaFree(da)); CUDA_OK(cudaFree(db)); CUDA_OK(cudaFree(dc));
    // Model names are diagnostic text; no hostname, address or credentials are emitted.
    std::printf("{\"phase\":\"F0\",\"performance_measurement\":false,"
                "\"cuda_devices\":%d,\"driver_api\":%d,\"runtime_api\":%d,"
                "\"compute_capability\":\"%d.%d\",\"verified_elements\":%d,"
                "\"result\":\"PASS\"}\n", count, driver, runtime, device.major, device.minor, n);
    return 0;
}
