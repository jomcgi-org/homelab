// Reference-quality CUDA kernels for the oominf CUDA backend.
//
// These favour clarity and fp32 arithmetic over speed: they are the correctness
// baseline that faster kernels are later checked against. Activations are fp32;
// weights arrive as bf16 (released dense weights) or NVFP4 records.

#include <cuda_bf16.h>
#include <cuda_fp8.h>
#include <stdint.h>

typedef __nv_bfloat16 bf16;

__device__ __forceinline__ float sigmoidf(float x) { return 1.0f / (1.0f + expf(-x)); }
__device__ __forceinline__ float siluf(float x) { return x * sigmoidf(x); }
// torch.nn.functional.softplus with beta=1, threshold=20.
__device__ __forceinline__ float softplusf(float x) { return x > 20.0f ? x : log1pf(expf(x)); }

// Block-wide sum for blockDim.x a multiple of 32 (<= 1024).
__device__ float block_sum(float v) {
    __shared__ float warp_sums[32];
    for (int o = 16; o > 0; o >>= 1) v += __shfl_xor_sync(0xffffffff, v, o);
    int lane = threadIdx.x & 31, warp = threadIdx.x >> 5;
    if (lane == 0) warp_sums[warp] = v;
    __syncthreads();
    int nwarps = blockDim.x >> 5;
    v = threadIdx.x < nwarps ? warp_sums[threadIdx.x] : 0.0f;
    if (warp == 0)
        for (int o = 16; o > 0; o >>= 1) v += __shfl_xor_sync(0xffffffff, v, o);
    __shared__ float total;
    if (threadIdx.x == 0) total = v;
    __syncthreads();
    float r = total;
    __syncthreads();
    return r;
}

extern "C" __global__ void f32_to_bf16(const float* x, bf16* y, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) y[i] = __float2bfloat16_rn(x[i]);
}

// RMSNorm over consecutive groups of `group` elements; one block per group.
// out = x * rsqrt(mean(x^2) + eps) * (plus_one + w), with w indexed over the whole row
// of `row` elements (row = groups_per_row * group).
extern "C" __global__ void rmsnorm_groups(const float* x, const bf16* w, float* out, int group,
                                          int row, float eps, float plus_one) {
    const float* xg = x + (size_t)blockIdx.x * group;
    float* og = out + (size_t)blockIdx.x * group;
    int woff = (int)(((size_t)blockIdx.x * group) % row);
    float ss = 0.0f;
    for (int i = threadIdx.x; i < group; i += blockDim.x) ss += xg[i] * xg[i];
    float inv = rsqrtf(block_sum(ss) / group + eps);
    for (int i = threadIdx.x; i < group; i += blockDim.x)
        og[i] = xg[i] * inv * (plus_one + __bfloat162float(w[woff + i]));
}

// y = silu(x * scale)
extern "C" __global__ void silu_scale(const float* x, float* y, float scale, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) y[i] = siluf(x[i] * scale);
}

// Hyper-connection mix: mixed[t,h] = mean_c sigmoid(up[t,c,h]) * normed[t,c,h].
extern "C" __global__ void hc_mix(const float* up, const float* normed, float* mixed, int T, int C,
                                  int H) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= T * H) return;
    int t = i / H, h = i % H;
    float acc = 0.0f;
    for (int c = 0; c < C; c++) {
        size_t j = (size_t)t * C * H + (size_t)c * H + h;
        acc += sigmoidf(up[j]) * normed[j];
    }
    mixed[i] = acc / C;
}

// Injection weights: inj = 2 * sigmoid(logit * inv_c).
extern "C" __global__ void hc_inject(const float* logit, float* inj, int n, float inv_c) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) inj[i] = 2.0f * sigmoidf(logit[i] * inv_c);
}

// Combine: out[t,c,h] = res[t,c,h] + y[t,h] * inj[t,c].
extern "C" __global__ void hc_combine(const float* res, const float* y, const float* inj, float* out,
                                      int T, int C, int H) {
    size_t i = (size_t)blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= (size_t)T * C * H) return;
    int h = i % H;
    int c = (i / H) % C;
    int t = i / ((size_t)C * H);
    out[i] = res[i] + y[(size_t)t * H + h] * inj[t * C + c];
}

// Depthwise causal conv1d + SiLU with a rolling state, one thread per channel.
// x: [T, D] pre-conv inputs. state: [D, K] last K inputs, oldest first (updated in
// place). w: [D, K]. out[t,d] = silu(sum_j w[d,j] * window[j]) where the window is the
// last K inputs including x[t].
extern "C" __global__ void causal_conv_silu(const float* x, float* state, const bf16* w, float* out,
                                            int T, int D, int K) {
    int d = blockIdx.x * blockDim.x + threadIdx.x;
    if (d >= D) return;
    float win[8];
    float wd[8];
    for (int j = 0; j < K; j++) {
        win[j] = state[(size_t)d * K + j];
        wd[j] = __bfloat162float(w[(size_t)d * K + j]);
    }
    for (int t = 0; t < T; t++) {
        for (int j = 0; j + 1 < K; j++) win[j] = win[j + 1];
        win[K - 1] = x[(size_t)t * D + d];
        float acc = 0.0f;
        for (int j = 0; j < K; j++) acc += wd[j] * win[j];
        out[(size_t)t * D + d] = siluf(acc);
    }
    for (int j = 0; j < K; j++) state[(size_t)d * K + j] = win[j];
}

// In-place L2 normalisation of `nheads` heads of width D at column `offset` of rows of
// `stride` floats: x *= rsqrt(sum(x^2) + eps). One warp per (t, head).
extern "C" __global__ void l2norm_heads(float* x, int T, int stride, int offset, int nheads, int D,
                                        float eps) {
    int warp = (blockIdx.x * blockDim.x + threadIdx.x) >> 5;
    int lane = threadIdx.x & 31;
    if (warp >= T * nheads) return;
    int t = warp / nheads, hd = warp % nheads;
    float* p = x + (size_t)t * stride + offset + (size_t)hd * D;
    float ss = 0.0f;
    for (int i = lane; i < D; i += 32) ss += p[i] * p[i];
    for (int o = 16; o > 0; o >>= 1) ss += __shfl_xor_sync(0xffffffff, ss, o);
    float inv = rsqrtf(ss + eps);
    for (int i = lane; i < D; i += 32) p[i] *= inv;
}

// GDN gates: g = -exp(A_log) * softplus(a + dt_bias), beta = sigmoid(b). a, b: [T, Hv].
extern "C" __global__ void gdn_gates(const float* a, const float* b, const bf16* a_log,
                                     const bf16* dt_bias, float* g, float* beta, int T, int Hv) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= T * Hv) return;
    int h = i % Hv;
    g[i] = -expf(__bfloat162float(a_log[h])) * softplusf(a[i] + __bfloat162float(dt_bias[h]));
    beta[i] = sigmoidf(b[i]);
}

#define GDN_DK 128
// Gated delta rule, token by token (exact recurrence; prefill and decode alike).
// One block per value head, one thread per value column j holding S[:, j] in registers.
// qkv: [T, stride] conv output with q at 0, k at Hk*Dk, v at 2*Hk*Dk (q, k already
// L2-normalised). state: [Hv, Dk, Dv] fp32, updated in place. out: [T, Hv, Dv].
extern "C" __global__ void __launch_bounds__(128)
gdn_recurrent(const float* qkv, const float* g, const float* beta, float* state, float* out, int T,
              int stride, int Hk, int Hv, int Dv, float scale) {
    int h = blockIdx.x, j = threadIdx.x;
    int kh = h / (Hv / Hk);
    __shared__ float qs[GDN_DK], ks[GDN_DK];
    float S[GDN_DK];
    float* st = state + (size_t)h * GDN_DK * Dv;
#pragma unroll
    for (int k = 0; k < GDN_DK; k++) S[k] = st[(size_t)k * Dv + j];
    for (int t = 0; t < T; t++) {
        const float* row = qkv + (size_t)t * stride;
        qs[j] = row[kh * GDN_DK + j] * scale;
        ks[j] = row[Hk * GDN_DK + kh * GDN_DK + j];
        __syncthreads();
        float v = row[2 * Hk * GDN_DK + h * Dv + j];
        float decay = expf(g[t * Hv + h]);
        float b = beta[t * Hv + h];
        float kv = 0.0f;
#pragma unroll
        for (int k = 0; k < GDN_DK; k++) {
            S[k] *= decay;
            kv += S[k] * ks[k];
        }
        float delta = (v - kv) * b;
        float o = 0.0f;
#pragma unroll
        for (int k = 0; k < GDN_DK; k++) {
            S[k] += ks[k] * delta;
            o += S[k] * qs[k];
        }
        out[((size_t)t * Hv + h) * Dv + j] = o;
        __syncthreads();
    }
#pragma unroll
    for (int k = 0; k < GDN_DK; k++) st[(size_t)k * Dv + j] = S[k];
}

// Gated RMSNorm per row of D: out = x * rsqrt(mean(x^2) + eps) * w * sigmoid(z).
// One warp per row; x, z, out: [rows, D].
extern "C" __global__ void gated_rmsnorm_sigmoid(const float* x, const float* z, const bf16* w,
                                                 float* out, int rows, int D, float eps) {
    int warp = (blockIdx.x * blockDim.x + threadIdx.x) >> 5;
    int lane = threadIdx.x & 31;
    if (warp >= rows) return;
    const float* xr = x + (size_t)warp * D;
    float ss = 0.0f;
    for (int i = lane; i < D; i += 32) ss += xr[i] * xr[i];
    for (int o = 16; o > 0; o >>= 1) ss += __shfl_xor_sync(0xffffffff, ss, o);
    float inv = rsqrtf(ss / D + eps);
    for (int i = lane; i < D; i += 32) {
        size_t k = (size_t)warp * D + i;
        out[k] = xr[i] * inv * __bfloat162float(w[i]) * sigmoidf(z[k]);
    }
}

// Router: softmax over E logits, top-k (ties to the lower id), renormalise.
// One block per token; thread 0 does the selection (E and k are small).
extern "C" __global__ void router_topk(const float* logits, int* ids, float* weights, int E,
                                       int k) {
    int t = blockIdx.x;
    const float* l = logits + (size_t)t * E;
    float m = -INFINITY;
    for (int e = threadIdx.x; e < E; e += blockDim.x) m = fmaxf(m, l[e]);
    __shared__ float red[1024];
    red[threadIdx.x] = m;
    __syncthreads();
    for (int s = blockDim.x / 2; s > 0; s >>= 1) {
        if (threadIdx.x < s) red[threadIdx.x] = fmaxf(red[threadIdx.x], red[threadIdx.x + s]);
        __syncthreads();
    }
    float mx = red[0];
    __syncthreads();
    float sum = 0.0f;
    for (int e = threadIdx.x; e < E; e += blockDim.x) sum += expf(l[e] - mx);
    sum = block_sum(sum);
    if (threadIdx.x != 0) return;
    float picked_sum = 0.0f;
    int chosen[32];
    float chosen_p[32];
    for (int r = 0; r < k; r++) {
        int best = -1;
        float bp = -1.0f;
        for (int e = 0; e < E; e++) {
            bool used = false;
            for (int q = 0; q < r; q++) used |= chosen[q] == e;
            if (used) continue;
            float p = expf(l[e] - mx) / sum;
            if (p > bp) { bp = p; best = e; }
        }
        chosen[r] = best;
        chosen_p[r] = bp;
        picked_sum += bp;
    }
    for (int r = 0; r < k; r++) {
        ids[t * k + r] = chosen[r];
        weights[t * k + r] = chosen_p[r] / picked_sum;
    }
}

// SwiGLU on separate gate and up outputs: y = silu(gate) * up.
extern "C" __global__ void silu_mul(const float* gate, const float* up, float* y, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) y[i] = siluf(gate[i]) * up[i];
}

// Fused gate/up layout [n, 2I] (gate then up per row): y[n, I] = silu(g) * u.
extern "C" __global__ void silu_mul_fused(const float* gu, float* y, int n, int I) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= n * I) return;
    int r = i / I, c = i % I;
    y[i] = siluf(gu[(size_t)r * 2 * I + c]) * gu[(size_t)r * 2 * I + I + c];
}

// moe = routed + sigmoid(gate_logit) * shared. gate_logit: [T].
extern "C" __global__ void moe_combine(const float* routed, const float* shared,
                                       const float* gate_logit, float* out, int T, int H) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= T * H) return;
    out[i] = routed[i] + sigmoidf(gate_logit[i / H]) * shared[i];
}

__device__ __forceinline__ float e2m1(uint8_t code) {
    const float mag[8] = {0.0f, 0.5f, 1.0f, 1.5f, 2.0f, 3.0f, 4.0f, 6.0f};
    float v = mag[code & 7];
    return (code & 8) ? -v : v;
}

// ModelOpt NVFP4 (group 16) to fp32: w[r, c] = e2m1(nibble) * fp8(scale[r, c/16]) * scale2,
// read straight from an expert record: packed [rows, cols/2] (low nibble = even column) at
// packed_off, e4m3 scales [rows, cols/16] at scale_off, and scale2 as the f32 at index
// scale2_idx of the record's leading scalars part.
extern "C" __global__ void dequant_nvfp4(const uint8_t* record, long long packed_off,
                                         long long scale_off, int scale2_idx, float* out,
                                         int rows, int cols) {
    size_t i = (size_t)blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= (size_t)rows * cols) return;
    const uint8_t* packed = record + packed_off;
    const uint8_t* scale = record + scale_off;
    float scale2 = ((const float*)record)[scale2_idx];
    int r = i / cols, c = i % cols;
    uint8_t byte = packed[(size_t)r * (cols / 2) + c / 2];
    uint8_t code = (c & 1) ? (byte >> 4) : (byte & 15);
    __nv_fp8_e4m3 s;
    s.__x = scale[(size_t)r * (cols / 16) + c / 16];
    out[i] = e2m1(code) * (float(s) * scale2);
}

// Gather rows: dst[i, :] = src[idx[i], :].
extern "C" __global__ void gather_rows(const float* src, const int* idx, float* dst, int n, int H) {
    size_t i = (size_t)blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= (size_t)n * H) return;
    int r = i / H, h = i % H;
    dst[i] = src[(size_t)idx[r] * H + h];
}

// Weighted scatter-add: dst[idx[i], :] += w[i] * src[i, :]. Rows of idx are distinct
// within one call (one expert sees each token at most once), so no atomics are needed.
extern "C" __global__ void scatter_add_weighted(const float* src, const int* idx, const float* w,
                                                float* dst, int n, int H) {
    size_t i = (size_t)blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= (size_t)n * H) return;
    int r = i / H, h = i % H;
    dst[(size_t)idx[r] * H + h] += w[r] * src[i];
}

extern "C" __global__ void add_inplace(float* x, const float* y, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) x[i] += y[i];
}

extern "C" __global__ void add_out(const float* x, const float* y, float* out, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) out[i] = x[i] + y[i];
}

// dst[0..n*H] = src rows [first, first + n) of width H.
extern "C" __global__ void copy_rows(const float* src, float* dst, int first, int n, int H) {
    size_t i = (size_t)blockIdx.x * blockDim.x + threadIdx.x;
    if (i < (size_t)n * H) dst[i] = src[(size_t)first * H + i];
}
