// Dense GEMV, normalisation, hyper-connection, recurrent and elementwise kernels.
// Activations, state and accumulation are fp32; dense weights are bf16 as released.

#include <cuda_bf16.h>
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
// x rows are x_stride floats apart (x_stride >= D), so x can be a column range of a
// fused projection.
extern "C" __global__ void causal_conv_silu(const float* x, int x_stride, float* state,
                                            const bf16* w, float* out, int T, int D, int K) {
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
        win[K - 1] = x[(size_t)t * x_stride + d];
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
// a and b rows are ab_stride floats apart (column ranges of a fused projection).
extern "C" __global__ void gdn_gates(const float* a, const float* b, int ab_stride,
                                     const bf16* a_log, const bf16* dt_bias, float* g,
                                     float* beta, int T, int Hv) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= T * Hv) return;
    int t = i / Hv, h = i % Hv;
    size_t j = (size_t)t * ab_stride + h;
    g[i] = -expf(__bfloat162float(a_log[h])) * softplusf(a[j] + __bfloat162float(dt_bias[h]));
    beta[i] = sigmoidf(b[j]);
}

#define GDN_DK 128
#define GDN_LANES 4
#define GDN_PART (GDN_DK / GDN_LANES)
// Gated delta rule, token by token (exact recurrence; prefill and decode alike).
// One block per value head, GDN_LANES adjacent lanes per value column j: lane l of the
// group holds S[l*32:(l+1)*32, j] in registers (no spills), and the partial dot
// products are combined with shuffles inside the group.
// qkv: [T, stride] conv output with q at 0, k at Hk*Dk, v at 2*Hk*Dk (q, k already
// L2-normalised). state: [Hv, Dk, Dv] fp32, updated in place. out: [T, Hv, Dv].
extern "C" __global__ void __launch_bounds__(512)
gdn_recurrent(const float* qkv, const float* g, const float* beta, float* state, float* out, int T,
              int stride, int Hk, int Hv, int Dv, float scale) {
    int h = blockIdx.x;
    int j = threadIdx.x / GDN_LANES, part = threadIdx.x % GDN_LANES;
    int kh = h / (Hv / Hk);
    int k0 = part * GDN_PART;
    __shared__ float qs[GDN_DK], ks[GDN_DK];
    float S[GDN_PART];
    float* st = state + (size_t)h * GDN_DK * Dv;
#pragma unroll
    for (int k = 0; k < GDN_PART; k++) S[k] = st[(size_t)(k0 + k) * Dv + j];
    for (int t = 0; t < T; t++) {
        const float* row = qkv + (size_t)t * stride;
        if (threadIdx.x < GDN_DK) {
            qs[threadIdx.x] = row[kh * GDN_DK + threadIdx.x] * scale;
            ks[threadIdx.x] = row[Hk * GDN_DK + kh * GDN_DK + threadIdx.x];
        }
        __syncthreads();
        float v = row[2 * Hk * GDN_DK + h * Dv + j];
        float decay = expf(g[t * Hv + h]);
        float b = beta[t * Hv + h];
        float kv = 0.0f;
#pragma unroll
        for (int k = 0; k < GDN_PART; k++) {
            S[k] *= decay;
            kv += S[k] * ks[k0 + k];
        }
#pragma unroll
        for (int o = 1; o < GDN_LANES; o <<= 1) kv += __shfl_xor_sync(0xffffffff, kv, o);
        float delta = (v - kv) * b;
        float o = 0.0f;
#pragma unroll
        for (int k = 0; k < GDN_PART; k++) {
            S[k] += ks[k0 + k] * delta;
            o += S[k] * qs[k0 + k];
        }
#pragma unroll
        for (int m = 1; m < GDN_LANES; m <<= 1) o += __shfl_xor_sync(0xffffffff, o, m);
        if (part == 0) out[((size_t)t * Hv + h) * Dv + j] = o;
        __syncthreads();
    }
#pragma unroll
    for (int k = 0; k < GDN_PART; k++) st[(size_t)(k0 + k) * Dv + j] = S[k];
}

// Gated RMSNorm per row of D: out = x * rsqrt(mean(x^2) + eps) * w * sigmoid(z).
// One warp per row; x, z, out: [rows, D].
// z: row r = t * per_token + i lives at z + t * z_stride + i * D (a column range of a
// fused projection; z_stride = per_token * D when compact).
extern "C" __global__ void gated_rmsnorm_sigmoid(const float* x, const float* z, int z_stride,
                                                 int per_token, const bf16* w, float* out,
                                                 int rows, int D, float eps) {
    int warp = (blockIdx.x * blockDim.x + threadIdx.x) >> 5;
    int lane = threadIdx.x & 31;
    if (warp >= rows) return;
    const float* xr = x + (size_t)warp * D;
    float ss = 0.0f;
    for (int i = lane; i < D; i += 32) ss += xr[i] * xr[i];
    for (int o = 16; o > 0; o >>= 1) ss += __shfl_xor_sync(0xffffffff, ss, o);
    float inv = rsqrtf(ss / D + eps);
    const float* zr = z + (size_t)(warp / per_token) * z_stride + (size_t)(warp % per_token) * D;
    for (int i = lane; i < D; i += 32) {
        size_t k = (size_t)warp * D + i;
        out[k] = xr[i] * inv * __bfloat162float(w[i]) * sigmoidf(zr[i]);
    }
}

// Router: softmax over E logits, top-k (ties to the lower id), renormalise.
// One block per token. Probabilities go to dynamic shared memory (E floats); each of
// the k rounds is a block-wide argmax with ties to the lower id, after which the
// winner is marked used. Same arithmetic as a serial scan: p = exp(l - max) / sum.
extern "C" __global__ void router_topk(const float* logits, int* ids, float* weights, int E,
                                       int k) {
    extern __shared__ float probs[];
    __shared__ float red_v[1024];
    __shared__ int red_i[1024];
    __shared__ float chosen_p[32];
    __shared__ int chosen[32];
    int t = blockIdx.x, tid = threadIdx.x;
    const float* l = logits + (size_t)t * E;
    float m = -INFINITY;
    for (int e = tid; e < E; e += blockDim.x) m = fmaxf(m, l[e]);
    red_v[tid] = m;
    __syncthreads();
    for (int s = blockDim.x / 2; s > 0; s >>= 1) {
        if (tid < s) red_v[tid] = fmaxf(red_v[tid], red_v[tid + s]);
        __syncthreads();
    }
    float mx = red_v[0];
    __syncthreads();
    float sum = 0.0f;
    for (int e = tid; e < E; e += blockDim.x) sum += expf(l[e] - mx);
    sum = block_sum(sum);
    for (int e = tid; e < E; e += blockDim.x) probs[e] = expf(l[e] - mx) / sum;
    __syncthreads();
    for (int r = 0; r < k; r++) {
        float bv = -3.0f;
        int bi = 0x7fffffff;
        for (int e = tid; e < E; e += blockDim.x) {
            float p = probs[e];
            if (p > bv || (p == bv && e < bi)) { bv = p; bi = e; }
        }
        red_v[tid] = bv;
        red_i[tid] = bi;
        __syncthreads();
        for (int s = blockDim.x / 2; s > 0; s >>= 1) {
            if (tid < s) {
                float ov = red_v[tid + s];
                int oi = red_i[tid + s];
                if (ov > red_v[tid] || (ov == red_v[tid] && oi < red_i[tid])) {
                    red_v[tid] = ov;
                    red_i[tid] = oi;
                }
            }
            __syncthreads();
        }
        if (tid == 0) {
            chosen[r] = red_i[0];
            chosen_p[r] = red_v[0];
            // Used entries sit below every probability (p >= 0), so they never win.
            probs[red_i[0]] = -2.0f;
        }
        __syncthreads();
    }
    if (tid != 0) return;
    float picked_sum = 0.0f;
    for (int r = 0; r < k; r++) picked_sum += chosen_p[r];
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

// SwiGLU on stacked rows: y[r, i] = silu(gu[r, i]) * gu[r, n + i], gu: [rows, 2n].
extern "C" __global__ void silu_mul_rows(const float* gu, float* y, int rows, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= rows * n) return;
    int r = i / n, c = i % n;
    y[i] = siluf(gu[(size_t)r * 2 * n + c]) * gu[(size_t)r * 2 * n + n + c];
}

// moe = routed + sigmoid(gate_logit) * shared. gate_logit: [T].
extern "C" __global__ void moe_combine(const float* routed, const float* shared,
                                       const float* gate_logit, float* out, int T, int H) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= T * H) return;
    out[i] = routed[i] + sigmoidf(gate_logit[i / H]) * shared[i];
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

// Decode GEMV: y[t, n] = sum_k x[t, k] * W[n, k] for T <= 4 tokens. x is fp32 (not
// rounded), W is bf16 [N, K] row-major, accumulation fp32. Each warp owns GEMV_R rows
// and streams them with 16-byte loads (8 bf16 per lane); x for this block's K range
// sits in shared memory. Split-K (gridDim.y > 1) writes per-split partials
// [splits, T, N]; the last block to finish a row tile (an atomic ticket per tile in
// `tickets`, reset after use) sums them in split order, so results are deterministic.
// K must be a multiple of 8; klen (K per split) a multiple of 256 except the last.
#define GEMV_R 4
template <int T>
__device__ void gemv_bf16_impl(const float* x, const bf16* W, float* y, float* partial,
                               unsigned* tickets, int N, int K, int klen) {
    extern __shared__ float xs[];
    int split = blockIdx.y;
    int k0 = split * klen;
    int k1 = min(K, k0 + klen);
    int kl = k1 - k0;
    for (int i = threadIdx.x; i < T * kl; i += blockDim.x) {
        int t = i / kl, kk = i % kl;
        xs[i] = x[(size_t)t * K + k0 + kk];
    }
    __syncthreads();
    int warp = threadIdx.x >> 5, lane = threadIdx.x & 31;
    int row0 = (blockIdx.x * (blockDim.x >> 5) + warp) * GEMV_R;
    float acc[T][GEMV_R];
#pragma unroll
    for (int t = 0; t < T; t++)
#pragma unroll
        for (int r = 0; r < GEMV_R; r++) acc[t][r] = 0.0f;
    for (int kb = lane * 8; row0 < N && kb < kl; kb += 256) {
        float xv[T][8];
#pragma unroll
        for (int t = 0; t < T; t++) {
            float4 a = *(const float4*)(xs + t * kl + kb);
            float4 b = *(const float4*)(xs + t * kl + kb + 4);
            xv[t][0] = a.x; xv[t][1] = a.y; xv[t][2] = a.z; xv[t][3] = a.w;
            xv[t][4] = b.x; xv[t][5] = b.y; xv[t][6] = b.z; xv[t][7] = b.w;
        }
#pragma unroll
        for (int r = 0; r < GEMV_R; r++) {
            int n = row0 + r;
            if (n >= N) break;
            uint4 raw = __ldg((const uint4*)(W + (size_t)n * K + k0 + kb));
            const __nv_bfloat162* w2 = (const __nv_bfloat162*)&raw;
            float w[8];
#pragma unroll
            for (int i = 0; i < 4; i++) {
                float2 f = __bfloat1622float2(w2[i]);
                w[2 * i] = f.x;
                w[2 * i + 1] = f.y;
            }
#pragma unroll
            for (int t = 0; t < T; t++)
#pragma unroll
                for (int i = 0; i < 8; i++) acc[t][r] += xv[t][i] * w[i];
        }
    }
#pragma unroll
    for (int t = 0; t < T; t++)
#pragma unroll
        for (int r = 0; r < GEMV_R; r++)
#pragma unroll
            for (int o = 16; o > 0; o >>= 1) acc[t][r] += __shfl_xor_sync(0xffffffff, acc[t][r], o);
    if (lane == 0) {
#pragma unroll
        for (int t = 0; t < T; t++)
#pragma unroll
            for (int r = 0; r < GEMV_R; r++) {
                int n = row0 + r;
                if (n >= N) break;
                if (partial)
                    partial[((size_t)split * T + t) * N + n] = acc[t][r];
                else
                    y[(size_t)t * N + n] = acc[t][r];
            }
    }
    if (!partial) return;
    // Last block of this row tile sums every split's partials in split order.
    __shared__ bool last;
    __threadfence();
    __syncthreads();
    if (threadIdx.x == 0) last = atomicAdd(&tickets[blockIdx.x], 1u) == gridDim.y - 1;
    __syncthreads();
    if (!last) return;
    __threadfence();
    int tile = (blockDim.x >> 5) * GEMV_R;
    int first = blockIdx.x * tile;
    for (int i = threadIdx.x; i < T * tile; i += blockDim.x) {
        int t = i / tile, n = first + i % tile;
        if (n >= N) continue;
        float sum = 0.0f;
        for (int s = 0; s < (int)gridDim.y; s++) sum += __ldcg(&partial[((size_t)s * T + t) * N + n]);
        y[(size_t)t * N + n] = sum;
    }
    if (threadIdx.x == 0) tickets[blockIdx.x] = 0;
}

extern "C" __global__ void __launch_bounds__(256) gemv_bf16_t1(const float* x, const bf16* W, float* y, float* partial, unsigned* tickets, int N, int K, int klen) { gemv_bf16_impl<1>(x, W, y, partial, tickets, N, K, klen); }
extern "C" __global__ void __launch_bounds__(256) gemv_bf16_t2(const float* x, const bf16* W, float* y, float* partial, unsigned* tickets, int N, int K, int klen) { gemv_bf16_impl<2>(x, W, y, partial, tickets, N, K, klen); }
extern "C" __global__ void __launch_bounds__(256) gemv_bf16_t3(const float* x, const bf16* W, float* y, float* partial, unsigned* tickets, int N, int K, int klen) { gemv_bf16_impl<3>(x, W, y, partial, tickets, N, K, klen); }
extern "C" __global__ void __launch_bounds__(256) gemv_bf16_t4(const float* x, const bf16* W, float* y, float* partial, unsigned* tickets, int N, int K, int klen) { gemv_bf16_impl<4>(x, W, y, partial, tickets, N, K, klen); }

// Inverse of copy_cols: dst[r, col .. col + cols] = src[r, :] for rows of `stride`.
extern "C" __global__ void put_cols(const float* src, float* dst, int rows, int stride, int col,
                                    int cols) {
    size_t i = (size_t)blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= (size_t)rows * cols) return;
    int r = i / cols, c = i % cols;
    dst[(size_t)r * stride + col + c] = src[i];
}

// Hyper-connection post-processing of the stacked [down (lr) | inject logits (hc)]
// projection (rows of `stride` floats): act = silu(down * inv_c), inject =
// 2 * sigmoid(logit * inv_c).
extern "C" __global__ void hc_post_down(const float* f, int stride, int lr, int hc, float inv_c,
                                        float* act, float* inject, int T) {
    int w = lr + hc;
    size_t i = (size_t)blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= (size_t)T * w) return;
    int t = i / w, c = i % w;
    float v = f[(size_t)t * stride + c] * inv_c;
    if (c < lr)
        act[(size_t)t * lr + c] = siluf(v);
    else
        inject[(size_t)t * hc + (c - lr)] = 2.0f * sigmoidf(v);
}
