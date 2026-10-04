// Fused routed-expert kernels over NVFP4 (ModelOpt, group 16) expert records.
//
// Precision (W4A16): every weight is decoded exactly as the reference does,
// w = e2m1(code) * (fp8_e4m3(scale) * weight_scale_2) in fp32; activations are fp32 and
// never quantised; products accumulate in fp32. Only the summation order differs from
// a dense fp32 GEMM.
//
// A step's routed work is a list of assignments (token, expert) grouped by expert:
// expert e owns assignments [off[e], off[e+1]). Records are addressed through a table
// of raw device pointers, one per expert of the step.

#include <cuda_fp8.h>
#include <stdint.h>

#define MOE_TB 8  // assignments processed together per weight decode

// E2M1 nibble to fp32 by bit construction (exact, no table): magnitude codes 0..7 are
// 0, 0.5, 1, 1.5, 2, 3, 4, 6; exponent field e = code >> 1, mantissa bit m = code & 1.
__device__ __forceinline__ float moe_e2m1(uint32_t code) {
    uint32_t e = (code >> 1) & 3, m = code & 1;
    uint32_t bits = e ? (((126u + e) << 23) | (m << 22)) : (m ? (126u << 23) : 0u);
    return __uint_as_float(bits | ((code & 8u) << 28));
}

__device__ __forceinline__ float moe_fp8(uint8_t b) {
    __nv_fp8_e4m3 s;
    s.__x = b;
    return float(s);
}

// Decodes one 16-weight group: 8 packed bytes (low nibble = even column) and its scale.
__device__ __forceinline__ void moe_decode16(const uint8_t* packed, uint8_t scale, float s2,
                                             float w[16]) {
    float gs = moe_fp8(scale) * s2;
    uint2 p = *reinterpret_cast<const uint2*>(packed);
    uint32_t words[2] = {p.x, p.y};
#pragma unroll
    for (int q = 0; q < 2; q++) {
#pragma unroll
        for (int n = 0; n < 8; n++) w[q * 8 + n] = moe_e2m1((words[q] >> (4 * n)) & 15) * gs;
    }
}

__device__ __forceinline__ float moe_warp_sum(float v) {
    for (int o = 16; o > 0; o >>= 1) v += __shfl_xor_sync(0xffffffff, v, o);
    return v;
}

__device__ __forceinline__ float moe_silu(float x) { return x / (1.0f + expf(-x)); }

// Gate and up projections plus SwiGLU: h[a, j] = silu(x_t . Wg[j]) * (x_t . Wu[j]) for
// every assignment a = (t, e). One warp per (expert, row j); grid (ceil(I / 8), n_experts),
// block 256. x: [T, H]; h: [A, I].
extern "C" __global__ void __launch_bounds__(256)
moe_gate_up(const unsigned long long* recs, const int* off, const int* assign_tok, const float* x,
            float* h, int H, int I, long long gw_off, long long gs_off, long long uw_off,
            long long us_off, int g_s2, int u_s2) {
    int e = blockIdx.y;
    int j = blockIdx.x * 8 + (threadIdx.x >> 5);
    int lane = threadIdx.x & 31;
    if (j >= I) return;
    const uint8_t* rec = reinterpret_cast<const uint8_t*>(recs[e]);
    float s2g = reinterpret_cast<const float*>(rec)[g_s2];
    float s2u = reinterpret_cast<const float*>(rec)[u_s2];
    const uint8_t* gw = rec + gw_off + (size_t)j * (H / 2);
    const uint8_t* gs = rec + gs_off + (size_t)j * (H / 16);
    const uint8_t* uw = rec + uw_off + (size_t)j * (H / 2);
    const uint8_t* us = rec + us_off + (size_t)j * (H / 16);
    int groups = H / 16;
    int a_end = off[e + 1];
    for (int a0 = off[e]; a0 < a_end; a0 += MOE_TB) {
        int nb = min(MOE_TB, a_end - a0);
        const float* xr[MOE_TB];
#pragma unroll
        for (int b = 0; b < MOE_TB; b++) xr[b] = x + (size_t)assign_tok[a0 + min(b, nb - 1)] * H;
        float ag[MOE_TB], au[MOE_TB];
#pragma unroll
        for (int b = 0; b < MOE_TB; b++) ag[b] = au[b] = 0.0f;
        for (int g = lane; g < groups; g += 32) {
            float wg[16], wu[16];
            moe_decode16(gw + g * 8, gs[g], s2g, wg);
            moe_decode16(uw + g * 8, us[g], s2u, wu);
#pragma unroll
            for (int b = 0; b < MOE_TB; b++) {
                if (b < nb) {
                    const float4* xv = reinterpret_cast<const float4*>(xr[b] + g * 16);
#pragma unroll
                    for (int q = 0; q < 4; q++) {
                        float4 v = xv[q];
                        ag[b] += wg[4 * q] * v.x + wg[4 * q + 1] * v.y + wg[4 * q + 2] * v.z +
                                 wg[4 * q + 3] * v.w;
                        au[b] += wu[4 * q] * v.x + wu[4 * q + 1] * v.y + wu[4 * q + 2] * v.z +
                                 wu[4 * q + 3] * v.w;
                    }
                }
            }
        }
#pragma unroll
        for (int b = 0; b < MOE_TB; b++) {
            float g = moe_warp_sum(ag[b]);
            float u = moe_warp_sum(au[b]);
            if (lane == 0 && b < nb) h[(size_t)(a0 + b) * I + j] = moe_silu(g) * u;
        }
    }
}

// Down projection: y[a, r] = h[a] . Wd[r] for every assignment. One warp per (expert,
// output row r); grid (ceil(H / 8), n_experts), block 256. h: [A, I]; y: [A, H].
extern "C" __global__ void __launch_bounds__(256)
moe_down(const unsigned long long* recs, const int* off, const float* h, float* y, int H, int I,
         long long dw_off, long long ds_off, int d_s2) {
    int e = blockIdx.y;
    int r = blockIdx.x * 8 + (threadIdx.x >> 5);
    int lane = threadIdx.x & 31;
    if (r >= H) return;
    const uint8_t* rec = reinterpret_cast<const uint8_t*>(recs[e]);
    float s2 = reinterpret_cast<const float*>(rec)[d_s2];
    const uint8_t* dw = rec + dw_off + (size_t)r * (I / 2);
    const uint8_t* ds = rec + ds_off + (size_t)r * (I / 16);
    int groups = I / 16;
    int a_end = off[e + 1];
    for (int a0 = off[e]; a0 < a_end; a0 += MOE_TB) {
        int nb = min(MOE_TB, a_end - a0);
        float acc[MOE_TB];
#pragma unroll
        for (int b = 0; b < MOE_TB; b++) acc[b] = 0.0f;
        for (int g = lane; g < groups; g += 32) {
            float w[16];
            moe_decode16(dw + g * 8, ds[g], s2, w);
#pragma unroll
            for (int b = 0; b < MOE_TB; b++) {
                if (b < nb) {
                    const float4* hv =
                        reinterpret_cast<const float4*>(h + (size_t)(a0 + b) * I + g * 16);
#pragma unroll
                    for (int q = 0; q < 4; q++) {
                        float4 v = hv[q];
                        acc[b] += w[4 * q] * v.x + w[4 * q + 1] * v.y + w[4 * q + 2] * v.z +
                                  w[4 * q + 3] * v.w;
                    }
                }
            }
        }
#pragma unroll
        for (int b = 0; b < MOE_TB; b++) {
            float s = moe_warp_sum(acc[b]);
            if (lane == 0 && b < nb) y[(size_t)(a0 + b) * H + r] = s;
        }
    }
}

// Weighted combine in fixed slot order (deterministic): out[t, c] =
// sum_s w[t, s] * y[slot_assign[t, s], c]. out: [T, H]; w, slot_assign: [T, k].
extern "C" __global__ void moe_combine_slots(const float* y, const int* slot_assign,
                                             const float* w, float* out, int T, int H, int k) {
    size_t i = (size_t)blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= (size_t)T * H) return;
    int t = i / H, c = i % H;
    float acc = 0.0f;
    for (int s = 0; s < k; s++)
        acc += w[t * k + s] * y[(size_t)slot_assign[t * k + s] * H + c];
    out[i] = acc;
}

// Tiled grouped NVFP4 GEMM for experts with many assignments (prefill):
// y[a, r] = x[row(a)] . W_e[r] over K, where row(a) = rows ? rows[a] : a. Block tile is
// 64 output rows x 32 assignments with K steps of 32; decoded weights and activations
// are staged in shared memory (fp32, exact decode). Grid (ceil(N/64), ceil(max_n/32),
// n_experts), block 256. x: [*, K]; y: [A, N].
extern "C" __global__ void __launch_bounds__(256)
moe_tiled(const unsigned long long* recs, const int* off, const int* rows, const float* x,
          float* y, int N, int K, long long w_off, long long s_off, int s2_idx) {
    __shared__ float ws[32][64];
    __shared__ float xs[32][33];
    int e = blockIdx.z;
    int a_begin = off[e] + blockIdx.y * 32;
    int a_end = off[e + 1];
    if (a_begin >= a_end) return;
    int nt = min(32, a_end - a_begin);
    int r0 = blockIdx.x * 64;
    const uint8_t* rec = reinterpret_cast<const uint8_t*>(recs[e]);
    float s2 = reinterpret_cast<const float*>(rec)[s2_idx];
    const uint8_t* wp = rec + w_off;
    const uint8_t* sp = rec + s_off;
    int tid = threadIdx.x;
    int tr = tid >> 4, tt = tid & 15;  // 16 row groups of 4 x 16 token groups of 2
    float acc[4][2] = {{0.0f}};
    // This thread's x-tile load: assignment (tid >> 3), 4 consecutive k at (tid & 7) * 4.
    int lt = tid >> 3, lk = (tid & 7) * 4;
    const float* xrow = nullptr;
    if (lt < nt) {
        int a = a_begin + lt;
        xrow = x + (size_t)(rows ? rows[a] : a) * K;
    }
    for (int k0 = 0; k0 < K; k0 += 32) {
        if (tid < 128) {
            int r = tid >> 1, grp = tid & 1;
            float w[16];
            if (r0 + r < N) {
                size_t row = (size_t)(r0 + r);
                int g = (k0 >> 4) + grp;
                moe_decode16(wp + row * (K / 2) + g * 8, sp[row * (K / 16) + g], s2, w);
            } else {
#pragma unroll
                for (int q = 0; q < 16; q++) w[q] = 0.0f;
            }
#pragma unroll
            for (int q = 0; q < 16; q++) ws[grp * 16 + q][r] = w[q];
        }
        float4 v = xrow ? *reinterpret_cast<const float4*>(xrow + k0 + lk)
                        : make_float4(0.f, 0.f, 0.f, 0.f);
        xs[lk][lt] = v.x;
        xs[lk + 1][lt] = v.y;
        xs[lk + 2][lt] = v.z;
        xs[lk + 3][lt] = v.w;
        __syncthreads();
#pragma unroll 8
        for (int kk = 0; kk < 32; kk++) {
            float4 wv = *reinterpret_cast<const float4*>(&ws[kk][tr * 4]);
            float b0 = xs[kk][tt * 2], b1 = xs[kk][tt * 2 + 1];
            acc[0][0] += wv.x * b0; acc[0][1] += wv.x * b1;
            acc[1][0] += wv.y * b0; acc[1][1] += wv.y * b1;
            acc[2][0] += wv.z * b0; acc[2][1] += wv.z * b1;
            acc[3][0] += wv.w * b0; acc[3][1] += wv.w * b1;
        }
        __syncthreads();
    }
#pragma unroll
    for (int i = 0; i < 4; i++) {
        int r = r0 + tr * 4 + i;
        if (r >= N) continue;
#pragma unroll
        for (int j = 0; j < 2; j++) {
            int t = tt * 2 + j;
            if (t < nt) y[(size_t)(a_begin + t) * N + r] = acc[i][j];
        }
    }
}

// h[a, j] = silu(g[a, j]) * u[a, j].
extern "C" __global__ void moe_swiglu(const float* g, const float* u, float* h, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) h[i] = moe_silu(g[i]) * u[i];
}
