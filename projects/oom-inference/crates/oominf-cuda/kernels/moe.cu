// Fused routed-expert kernels over NVFP4 (ModelOpt, group 16) and bf16 expert records.
//
// Precision (W4A16): every weight is decoded exactly as the reference does,
// w = e2m1(code) * (fp8_e4m3(scale) * weight_scale_2) in fp32; activations are fp32 and
// never quantised; products accumulate in fp32. Only the summation order differs from
// a dense fp32 GEMM.
//
// A step's routed work is a list of assignments (token, expert) grouped by expert:
// expert e owns assignments [off[e], off[e+1]). Records are addressed through a table
// of raw device pointers, one per expert of the step.

#include <cuda_bf16.h>
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

// Tiled grouped NVFP4 GEMM on tensor cores for experts with many assignments (prefill):
// y[a, r] = x[row(a)] . W_e[r] over K, where row(a) = rows ? rows[a] : a.
//
// Exact in the sense of the header: e2m1(code) * fp8(scale) has at most 6 significant
// bits, so it is a bf16 value with no rounding, and weight_scale_2 multiplies the fp32
// result instead of every weight. Each fp32 activation is split into three bf16 terms
// (hi + mid + lo == x exactly), every bf16 x bf16 product is exact in fp32, and the
// mma accumulates in fp32: an fp32 GEMM in another summation order, three tensor-core
// passes instead of fp32 FMAs.
//
// Block tile: 128 weight rows x 32 assignments, K steps of 32; four warps, each owning
// 32 rows (4 n8 tiles) x 32 assignments (2 m16 tiles). Grid (ceil(N/128),
// ceil(max_n/32), n_experts), block 128. x: [*, K]; y: [A, N].
#define MMA_BN 128
#define MMA_BM 32
#define MMA_STRIDE 40  // bf16 per shared row: 32 + 8 padding keeps fragment loads conflict-free

__device__ __forceinline__ uint16_t moe_bf16_bits(float v) {
    __nv_bfloat16 b = __float2bfloat16_rn(v);
    return *reinterpret_cast<uint16_t*>(&b);
}

__device__ __forceinline__ float moe_bf16_value(uint16_t bits) {
    return __uint_as_float(uint32_t(bits) << 16);
}

__device__ __forceinline__ void moe_mma_bf16(float c[4], const uint32_t a[4], const uint32_t b[2]) {
    asm volatile(
        "mma.sync.aligned.m16n8k16.row.col.f32.bf16.bf16.f32 "
        "{%0, %1, %2, %3}, {%4, %5, %6, %7}, {%8, %9}, {%0, %1, %2, %3};\n"
        : "+f"(c[0]), "+f"(c[1]), "+f"(c[2]), "+f"(c[3])
        : "r"(a[0]), "r"(a[1]), "r"(a[2]), "r"(a[3]), "r"(b[0]), "r"(b[1]));
}

extern "C" __global__ void __launch_bounds__(128)
moe_tiled(const unsigned long long* recs, const int* off, const int* rows, const float* x,
          float* y, int N, int K, long long w_off, long long s_off, int s2_idx) {
    __shared__ __align__(16) uint16_t ws[MMA_BN][MMA_STRIDE];
    __shared__ __align__(16) uint16_t xs[3][MMA_BM][MMA_STRIDE];
    int e = blockIdx.z;
    int a_begin = off[e] + blockIdx.y * MMA_BM;
    int a_end = off[e + 1];
    if (a_begin >= a_end) return;
    int nt = min(MMA_BM, a_end - a_begin);
    int r0 = blockIdx.x * MMA_BN;
    const uint8_t* rec = reinterpret_cast<const uint8_t*>(recs[e]);
    float s2 = reinterpret_cast<const float*>(rec)[s2_idx];
    const uint8_t* wp = rec + w_off;
    const uint8_t* sp = rec + s_off;
    int tid = threadIdx.x, warp = tid >> 5, lane = tid & 31;
    int g = lane >> 2, q = lane & 3;
    float acc[2][4][4] = {};
    // Weight decode: thread tid owns weight row r0 + tid.
    int wrow = r0 + tid;
    // Activation loads: two float4 per thread, assignment idx >> 3, k (idx & 7) * 4.
    const float* xrow[2];
#pragma unroll
    for (int i = 0; i < 2; i++) {
        int t = (tid + i * 128) >> 3;
        xrow[i] = t < nt ? x + (size_t)(rows ? rows[a_begin + t] : a_begin + t) * K : nullptr;
    }
    for (int k0 = 0; k0 < K; k0 += 32) {
#pragma unroll
        for (int grp = 0; grp < 2; grp++) {
            uint32_t packed[8];
            if (wrow < N) {
                size_t row = (size_t)wrow;
                int gi = (k0 >> 4) + grp;
                float w[16];
                moe_decode16(wp + row * (K / 2) + gi * 8, sp[row * (K / 16) + gi], 1.0f, w);
#pragma unroll
                for (int n = 0; n < 8; n++)
                    packed[n] = uint32_t(moe_bf16_bits(w[2 * n])) |
                                (uint32_t(moe_bf16_bits(w[2 * n + 1])) << 16);
            } else {
#pragma unroll
                for (int n = 0; n < 8; n++) packed[n] = 0;
            }
            uint4* dst = reinterpret_cast<uint4*>(&ws[tid][grp * 16]);
            dst[0] = make_uint4(packed[0], packed[1], packed[2], packed[3]);
            dst[1] = make_uint4(packed[4], packed[5], packed[6], packed[7]);
        }
#pragma unroll
        for (int i = 0; i < 2; i++) {
            int idx = tid + i * 128, t = idx >> 3, kq = (idx & 7) * 4;
            float4 v = xrow[i] ? *reinterpret_cast<const float4*>(xrow[i] + k0 + kq)
                               : make_float4(0.f, 0.f, 0.f, 0.f);
            float vals[4] = {v.x, v.y, v.z, v.w};
            uint16_t parts[3][4];
#pragma unroll
            for (int c = 0; c < 4; c++) {
                uint16_t hi = moe_bf16_bits(vals[c]);
                float r = vals[c] - moe_bf16_value(hi);
                uint16_t mid = moe_bf16_bits(r);
                uint16_t lo = moe_bf16_bits(r - moe_bf16_value(mid));
                parts[0][c] = hi;
                parts[1][c] = mid;
                parts[2][c] = lo;
            }
#pragma unroll
            for (int p = 0; p < 3; p++)
                *reinterpret_cast<uint2*>(&xs[p][t][kq]) =
                    make_uint2(uint32_t(parts[p][0]) | (uint32_t(parts[p][1]) << 16),
                               uint32_t(parts[p][2]) | (uint32_t(parts[p][3]) << 16));
        }
        __syncthreads();
#pragma unroll
        for (int kk = 0; kk < 32; kk += 16) {
            uint32_t b[4][2];
#pragma unroll
            for (int j = 0; j < 4; j++) {
                const uint16_t* wr = &ws[warp * 32 + j * 8 + g][kk + q * 2];
                b[j][0] = *reinterpret_cast<const uint32_t*>(wr);
                b[j][1] = *reinterpret_cast<const uint32_t*>(wr + 8);
            }
#pragma unroll
            for (int p = 0; p < 3; p++) {
#pragma unroll
                for (int m = 0; m < 2; m++) {
                    const uint16_t* x0 = &xs[p][m * 16 + g][kk + q * 2];
                    const uint16_t* x8 = &xs[p][m * 16 + g + 8][kk + q * 2];
                    uint32_t a[4] = {*reinterpret_cast<const uint32_t*>(x0),
                                     *reinterpret_cast<const uint32_t*>(x8),
                                     *reinterpret_cast<const uint32_t*>(x0 + 8),
                                     *reinterpret_cast<const uint32_t*>(x8 + 8)};
#pragma unroll
                    for (int j = 0; j < 4; j++) moe_mma_bf16(acc[m][j], a, b[j]);
                }
            }
        }
        __syncthreads();
    }
#pragma unroll
    for (int m = 0; m < 2; m++) {
#pragma unroll
        for (int j = 0; j < 4; j++) {
            int r = r0 + warp * 32 + j * 8 + q * 2;
#pragma unroll
            for (int h = 0; h < 2; h++) {
                int t = m * 16 + g + h * 8;
                if (t >= nt) continue;
                float* out = y + (size_t)(a_begin + t) * N;
                if (r < N) out[r] = acc[m][j][2 * h] * s2;
                if (r + 1 < N) out[r + 1] = acc[m][j][2 * h + 1] * s2;
            }
        }
    }
}

// h[a, j] = silu(g[a, j]) * u[a, j].
extern "C" __global__ void moe_swiglu(const float* g, const float* u, float* h, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) h[i] = moe_silu(g[i]) * u[i];
}

// bf16 expert records (weights as released, row-major): the same decode kernels as
// above with 8-weight groups read as one 16-byte load and widened exactly to fp32.
__device__ __forceinline__ void moe_bf16x8(const uint4 v, float w[8]) {
    const uint32_t words[4] = {v.x, v.y, v.z, v.w};
#pragma unroll
    for (int q = 0; q < 4; q++) {
        w[2 * q] = __uint_as_float(words[q] << 16);
        w[2 * q + 1] = __uint_as_float(words[q] & 0xffff0000u);
    }
}

// Gate and up projections plus SwiGLU over bf16 records; layout as moe_gate_up.
extern "C" __global__ void __launch_bounds__(256)
moe_gate_up_bf16(const unsigned long long* recs, const int* off, const int* assign_tok,
                 const float* x, float* h, int H, int I, long long gw_off, long long uw_off) {
    int e = blockIdx.y;
    int j = blockIdx.x * 8 + (threadIdx.x >> 5);
    int lane = threadIdx.x & 31;
    if (j >= I) return;
    const uint8_t* rec = reinterpret_cast<const uint8_t*>(recs[e]);
    const uint4* gw = reinterpret_cast<const uint4*>(rec + gw_off + (size_t)j * H * 2);
    const uint4* uw = reinterpret_cast<const uint4*>(rec + uw_off + (size_t)j * H * 2);
    int groups = H / 8;
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
            float wg[8], wu[8];
            moe_bf16x8(gw[g], wg);
            moe_bf16x8(uw[g], wu);
#pragma unroll
            for (int b = 0; b < MOE_TB; b++) {
                if (b < nb) {
                    const float4* xv = reinterpret_cast<const float4*>(xr[b] + g * 8);
#pragma unroll
                    for (int q = 0; q < 2; q++) {
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

// Down projection over bf16 records; layout as moe_down.
extern "C" __global__ void __launch_bounds__(256)
moe_down_bf16(const unsigned long long* recs, const int* off, const float* h, float* y, int H,
              int I, long long dw_off) {
    int e = blockIdx.y;
    int r = blockIdx.x * 8 + (threadIdx.x >> 5);
    int lane = threadIdx.x & 31;
    if (r >= H) return;
    const uint8_t* rec = reinterpret_cast<const uint8_t*>(recs[e]);
    const uint4* dw = reinterpret_cast<const uint4*>(rec + dw_off + (size_t)r * I * 2);
    int groups = I / 8;
    int a_end = off[e + 1];
    for (int a0 = off[e]; a0 < a_end; a0 += MOE_TB) {
        int nb = min(MOE_TB, a_end - a0);
        float acc[MOE_TB];
#pragma unroll
        for (int b = 0; b < MOE_TB; b++) acc[b] = 0.0f;
        for (int g = lane; g < groups; g += 32) {
            float w[8];
            moe_bf16x8(dw[g], w);
#pragma unroll
            for (int b = 0; b < MOE_TB; b++) {
                if (b < nb) {
                    const float4* hv =
                        reinterpret_cast<const float4*>(h + (size_t)(a0 + b) * I + g * 8);
#pragma unroll
                    for (int q = 0; q < 2; q++) {
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
