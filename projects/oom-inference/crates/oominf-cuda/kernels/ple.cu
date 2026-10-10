// Per-layer embedding (PLE) kernels. All arithmetic is fp32.

#include <cuda_bf16.h>
#include <cuda_fp8.h>
#include <stdint.h>

typedef __nv_bfloat16 bf16;

__device__ __forceinline__ float ple_sigmoidf(float x) { return 1.0f / (1.0f + expf(-x)); }

// FP8 E4M3 rows to fp32: out[i] = fp8(rows[i]) * scale.
extern "C" __global__ void fp8_dequant_scaled(const uint8_t* rows, float scale, float* out, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= n) return;
    __nv_fp8_e4m3 v;
    v.__x = rows[i];
    out[i] = float(v) * scale;
}

// Key/query gate per (token, stream), one block each:
//   g = sum_h key[t,c,h] * query[t,c,h] * inv_sqrt_h
//   g = sign(g) * sqrt(max(|g|, 1e-6))
//   gated[t,c,h] = sigmoid(g) * value[t,h]
extern "C" __global__ void ple_gate(const float* key, const float* query, const float* value,
                                    float* gated, int C, int H, float inv_sqrt_h) {
    int t = blockIdx.x / C, c = blockIdx.x % C;
    size_t base = ((size_t)t * C + c) * H;
    float acc = 0.0f;
    for (int h = threadIdx.x; h < H; h += blockDim.x) acc += key[base + h] * query[base + h];
    __shared__ float warp_sums[32];
    for (int o = 16; o > 0; o >>= 1) acc += __shfl_xor_sync(0xffffffff, acc, o);
    int lane = threadIdx.x & 31, warp = threadIdx.x >> 5;
    if (lane == 0) warp_sums[warp] = acc;
    __syncthreads();
    __shared__ float gate;
    if (threadIdx.x == 0) {
        float s = 0.0f;
        for (int w = 0; w < (int)(blockDim.x >> 5); w++) s += warp_sums[w];
        float g = s * inv_sqrt_h;
        float sgn = (g > 0.0f) - (g < 0.0f);
        gate = ple_sigmoidf(sqrtf(fmaxf(fabsf(g), 1e-6f)) * sgn);
    }
    __syncthreads();
    for (int h = threadIdx.x; h < H; h += blockDim.x)
        gated[base + h] = gate * value[(size_t)t * H + h];
}

#define PLE_MAX_STATE 32
// Dilated depthwise causal conv + SiLU, added to `gated`, one thread per channel:
//   out[t,d] = gated[t,d] + silu(sum_j w[d,j] * x[t - (K-1-j)*dil, d])
// x: [T, D] conv inputs; state: [D, S] previous S = (K-1)*dil inputs, oldest first,
// updated in place. w: [D, K].
extern "C" __global__ void dilated_conv_silu_add(const float* x, float* state, const bf16* w,
                                                 const float* gated, float* out, int T, int D,
                                                 int K, int dil) {
    int d = blockIdx.x * blockDim.x + threadIdx.x;
    if (d >= D) return;
    int S = (K - 1) * dil;
    float buf[PLE_MAX_STATE];
    for (int i = 0; i < S; i++) buf[i] = state[(size_t)d * S + i];
    for (int t = 0; t < T; t++) {
        float xt = x[(size_t)t * D + d];
        float acc = __bfloat162float(w[(size_t)d * K + K - 1]) * xt;
        for (int j = 0; j + 1 < K; j++)
            acc += __bfloat162float(w[(size_t)d * K + j]) * buf[S - (K - 1 - j) * dil];
        size_t k = (size_t)t * D + d;
        out[k] = gated[k] + acc / (1.0f + expf(-acc));
        for (int i = 0; i + 1 < S; i++) buf[i] = buf[i + 1];
        if (S > 0) buf[S - 1] = xt;
    }
    for (int i = 0; i < S; i++) state[(size_t)d * S + i] = buf[i];
}
