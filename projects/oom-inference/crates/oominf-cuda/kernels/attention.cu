// Reference-quality kernels for full attention with the QSA sparse indexer.
// All arithmetic is fp32.

#include <math.h>
#include <stdint.h>

__device__ __forceinline__ float attn_sigmoidf(float x) { return 1.0f / (1.0f + expf(-x)); }

// Splits the fused query/gate projection [T, H, 2*D] into q [T, H, D] and gate [T, H*D].
extern "C" __global__ void split_q_gate(const float* qg, float* q, float* gate, int T, int H, int D) {
    size_t i = (size_t)blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= (size_t)T * H * D) return;
    int d = i % D;
    size_t th = i / D;  // t * H + h
    q[i] = qg[th * 2 * D + d];
    gate[i] = qg[th * 2 * D + D + d];
}

// Copies `cols` columns starting at `col` from each row of `src` [rows, stride] into
// dst [rows, cols].
extern "C" __global__ void copy_cols(const float* src, float* dst, int rows, int stride, int col,
                                     int cols) {
    size_t i = (size_t)blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= (size_t)rows * cols) return;
    int r = i / cols, c = i % cols;
    dst[i] = src[(size_t)r * stride + col + c];
}

// Rotate-half RoPE on the first `rd` dims of each head row (NeoX layout: pairs (i, i + rd/2)),
// leaving the remaining dims untouched. x: [ntok, nheads, D]; token i has position
// pos_base + i * pos_stride. inv_freq: [rd / 2].
extern "C" __global__ void rope_rotate_half(float* x, int ntok, int nheads, int D, int rd,
                                            const float* inv_freq, int pos_base, int pos_stride) {
    int half = rd / 2;
    size_t i = (size_t)blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= (size_t)ntok * nheads * half) return;
    int f = i % half;
    size_t row = i / half;  // tok * nheads + head
    int tok = row / nheads;
    float pos = (float)(pos_base + tok * pos_stride);
    float ang = pos * inv_freq[f];
    float c = cosf(ang), s = sinf(ang);
    float* p = x + row * D;
    float x1 = p[f], x2 = p[f + half];
    p[f] = x1 * c - x2 * s;
    p[f + half] = x2 * c + x1 * s;
}

// Mean of each consecutive group of `ratio` rows: out[b, :] = mean(raw[b*ratio .. +ratio, :]).
extern "C" __global__ void pool_rows(const float* raw, float* out, int nblocks, int ratio, int D) {
    size_t i = (size_t)blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= (size_t)nblocks * D) return;
    int b = i / D, d = i % D;
    float acc = 0.0f;
    for (int r = 0; r < ratio; r++) acc += raw[((size_t)b * ratio + r) * D + d];
    out[i] = acc / ratio;
}

// QSA indexer selection. One block per query token t (absolute position start + t).
// Block-wide reductions for blockDim.x == 256.
__device__ float attn_block_max(float v, float* red) {
    for (int o = 16; o > 0; o >>= 1) v = fmaxf(v, __shfl_xor_sync(0xffffffff, v, o));
    if ((threadIdx.x & 31) == 0) red[threadIdx.x >> 5] = v;
    __syncthreads();
    v = threadIdx.x < 8 ? red[threadIdx.x] : -INFINITY;
    if (threadIdx.x < 32)
        for (int o = 4; o > 0; o >>= 1) v = fmaxf(v, __shfl_xor_sync(0xffffffff, v, o));
    if (threadIdx.x == 0) red[8] = v;
    __syncthreads();
    float r = red[8];
    __syncthreads();
    return r;
}

__device__ float attn_block_sum(float v, float* red) {
    for (int o = 16; o > 0; o >>= 1) v += __shfl_xor_sync(0xffffffff, v, o);
    if ((threadIdx.x & 31) == 0) red[threadIdx.x >> 5] = v;
    __syncthreads();
    v = threadIdx.x < 8 ? red[threadIdx.x] : 0.0f;
    if (threadIdx.x < 32)
        for (int o = 4; o > 0; o >>= 1) v += __shfl_xor_sync(0xffffffff, v, o);
    if (threadIdx.x == 0) red[8] = v;
    __syncthreads();
    float r = red[8];
    __syncthreads();
    return r;
}

// QSA block scores. Block b covers key positions [b*ratio, (b+1)*ratio); query t at
// position p = start + t sees nb = (p+1)/ratio complete blocks, and
// score[t, b] = sum_h relu(q[t,h] . kb[b]) / sqrt(Di). Row stride: kv_stride/ratio + 1.
extern "C" __global__ void qsa_scores(const float* q, const float* kb, float* scores, int T,
                                      int start, int nheads, int Di, int ratio, int kv_stride) {
    int t = blockIdx.x;
    int nb = (start + t + 1) / ratio;
    float* sc = scores + (size_t)t * (kv_stride / ratio + 1);
    const float* qt = q + (size_t)t * nheads * Di;
    float div = sqrtf((float)Di);
    for (int b = threadIdx.x; b < nb; b += blockDim.x) {
        float s = 0.0f;
        for (int h = 0; h < nheads; h++) {
            float dot = 0.0f;
            for (int d = 0; d < Di; d++) dot += qt[h * Di + d] * kb[(size_t)b * Di + d];
            s += fmaxf(dot, 0.0f);
        }
        sc[b] = s / div;
    }
}

// Order-preserving map from float to unsigned (larger float, larger key).
__device__ __forceinline__ unsigned qsa_key(float f) {
    unsigned u = __float_as_uint(f);
    return (u & 0x80000000u) ? ~u : (u | 0x80000000u);
}

// QSA selection mask from block scores: per query, keep the top `topk` complete blocks
// (equal scores go to the lower block index) and the incomplete tail; every block when
// nb <= topk. mask: [T, kv_stride] bytes, 1 where key position j (j <= p) is kept.
// The k-th largest score is found by an 8-bit radix select over its float key, so a
// query costs O(nb) rather than O(nb^2). Dynamic shared memory: kv_stride/ratio + 1
// bytes for the per-block keep flags. blockDim.x == 256.
extern "C" __global__ void __launch_bounds__(256)
qsa_mask(const float* scores, uint8_t* mask, int T, int start, int ratio, int topk,
         int kv_stride) {
    extern __shared__ uint8_t keep[];
    __shared__ unsigned hist[256];
    __shared__ unsigned s_prefix, s_k;
    int t = blockIdx.x;
    int p = start + t;
    int nb = (p + 1) / ratio;
    const float* sc = scores + (size_t)t * (kv_stride / ratio + 1);
    if (nb > topk) {
        unsigned prefix = 0, pmask = 0, k = topk;
        for (int shift = 24; shift >= 0; shift -= 8) {
            hist[threadIdx.x] = 0;
            __syncthreads();
            for (int b = threadIdx.x; b < nb; b += blockDim.x) {
                unsigned key = qsa_key(sc[b]);
                if ((key & pmask) == prefix) atomicAdd(&hist[(key >> shift) & 255u], 1u);
            }
            __syncthreads();
            if (threadIdx.x == 0) {
                // Walk bins from the largest digit down to the one holding the k-th key.
                unsigned acc = 0;
                int bin = 255;
                for (; bin > 0; bin--) {
                    if (acc + hist[bin] >= k) break;
                    acc += hist[bin];
                }
                s_k = k - acc;
                s_prefix = prefix | ((unsigned)bin << shift);
            }
            __syncthreads();
            prefix = s_prefix;
            k = s_k;
            pmask |= 255u << shift;
        }
        // `prefix` is now the k-th largest key exactly; keep everything above it, then
        // the first `k` equal keys in block order.
        for (int b = threadIdx.x; b < nb; b += blockDim.x) keep[b] = qsa_key(sc[b]) > prefix;
        __syncthreads();
        if (threadIdx.x == 0) {
            unsigned taken = 0;
            for (int b = 0; b < nb && taken < k; b++)
                if (qsa_key(sc[b]) == prefix) {
                    keep[b] = 1;
                    taken++;
                }
        }
    } else {
        for (int b = threadIdx.x; b < nb; b += blockDim.x) keep[b] = 1;
    }
    __syncthreads();
    uint8_t* m = mask + (size_t)t * kv_stride;
    for (int j = threadIdx.x; j < kv_stride; j += blockDim.x) {
        int b = j / ratio;
        m[j] = j <= p && (b >= nb || keep[b]);
    }
}

// Masked GQA attention for several query tokens with an online softmax over 256-key
// tiles, so no [T, H, kv] score matrix exists. One block (256 threads) per (t, h);
// thread i scores key i of the tile, then owns output element d = i (D <= 256).
// q: [T, H, D]; k, v caches: [kv_len_max, Hkv, D]; mask: [T, kv_stride]; out: [T, H, D].
extern "C" __global__ void __launch_bounds__(256)
attn_prefill(const float* q, const float* k, const float* v, const uint8_t* mask, float* out,
             int T, int H, int Hkv, int D, int kv_len, int kv_stride, float scale) {
    __shared__ float qs[256];
    __shared__ float ps[256];
    __shared__ float red[9];
    int t = blockIdx.x / H, h = blockIdx.x % H;
    int kvh = h / (H / Hkv);
    int i = threadIdx.x;
    qs[i] = i < D ? q[((size_t)t * H + h) * D + i] : 0.0f;
    const uint8_t* mrow = mask + (size_t)t * kv_stride;
    float m = -INFINITY, l = 0.0f, acc = 0.0f;
    __syncthreads();
    for (int j0 = 0; j0 < kv_len; j0 += 256) {
        int j = j0 + i;
        float s = -INFINITY;
        if (j < kv_len && mrow[j]) {
            const float* kr = k + ((size_t)j * Hkv + kvh) * D;
            float dot = 0.0f;
            for (int d = 0; d < D; d++) dot += qs[d] * kr[d];
            s = dot * scale;
        }
        float tmax = attn_block_max(s, red);
        if (tmax == -INFINITY) continue;  // whole tile masked (uniform across the block)
        float mn = fmaxf(m, tmax);
        float corr = expf(m - mn);
        float e = s == -INFINITY ? 0.0f : expf(s - mn);
        ps[i] = e;
        float tsum = attn_block_sum(e, red);
        l = l * corr + tsum;
        if (i < D) {
            float a = 0.0f;
            int n = min(256, kv_len - j0);
            for (int jj = 0; jj < n; jj++) {
                float pj = ps[jj];
                if (pj != 0.0f) a += pj * v[((size_t)(j0 + jj) * Hkv + kvh) * D + i];
            }
            acc = acc * corr + a;
        }
        m = mn;
        __syncthreads();  // ps is rewritten by the next tile
    }
    if (i < D) out[((size_t)t * H + h) * D + i] = acc / l;
}

// x *= sigmoid(gate), elementwise.
extern "C" __global__ void mul_sigmoid(float* x, const float* gate, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) x[i] *= attn_sigmoidf(gate[i]);
}

// dst[offset + i] = src[i]
extern "C" __global__ void copy_at(const float* src, float* dst, size_t offset, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) dst[offset + i] = src[i];
}

// Swaps the first two axes: dst[b, a, :] = src[a, b, :] for src [A, B, D].
extern "C" __global__ void swap01(const float* src, float* dst, int A, int B, int D) {
    size_t i = (size_t)blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= (size_t)A * B * D) return;
    int d = i % D;
    size_t ab = i / D;
    int b = ab % B, a = ab / B;
    dst[((size_t)b * A + a) * D + d] = src[i];
}

// dst[dst_off + i] = src[src_off + i] for i < n.
extern "C" __global__ void copy_range(const float* src, size_t src_off, float* dst, size_t dst_off,
                                      int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) dst[dst_off + i] = src[src_off + i];
}

// Flash-decode for one query token (T = 1), GQA-aware. Each warp owns a contiguous
// chunk of `chunk` keys and all G = H / Hkv query heads that share KV head
// blockIdx.y, so each K/V row is read once per group. Per (warp, head) it keeps an
// online-softmax partial (max m, sum l, unnormalised acc[D]); attn_decode_combine
// merges the partials. D is 256: lane `l` owns elements l, l + 32, ..., so global
// and shared loads are both conflict-free. G is a compile-time constant so the
// accumulators stay in registers. Masked keys are skipped.
// part: [Hkv, P, G, D + 2] with P = gridDim.x * warps per block.
#define FD_D 256
template <int G>
__device__ void attn_decode_partial_impl(const float* q, const float* k, const float* v,
                                         const uint8_t* mask, float* part, int Hkv,
                                         int kv_len, int chunk, float scale) {
    __shared__ float qs[G * FD_D];
    int warp = threadIdx.x >> 5, lane = threadIdx.x & 31;
    int wpb = blockDim.x >> 5;
    int kvh = blockIdx.y;
    int P = gridDim.x * wpb;
    int p = blockIdx.x * wpb + warp;
    for (int i = threadIdx.x; i < G * FD_D; i += blockDim.x) qs[i] = q[(size_t)kvh * G * FD_D + i];
    __syncthreads();
    float acc[G][8];
    float m[G], l[G];
#pragma unroll
    for (int g = 0; g < G; g++) {
        m[g] = -INFINITY;
        l[g] = 0.0f;
#pragma unroll
        for (int i = 0; i < 8; i++) acc[g][i] = 0.0f;
    }
    int j0 = p * chunk;
    int j1 = min(kv_len, j0 + chunk);
    for (int j = j0; j < j1; j++) {
        if (!mask[j]) continue;
        const float* kr = k + ((size_t)j * Hkv + kvh) * FD_D;
        const float* vr = v + ((size_t)j * Hkv + kvh) * FD_D;
        float kk[8], vv[8];
#pragma unroll
        for (int i = 0; i < 8; i++) {
            kk[i] = kr[i * 32 + lane];
            vv[i] = vr[i * 32 + lane];
        }
#pragma unroll
        for (int g = 0; g < G; g++) {
            float dot = 0.0f;
#pragma unroll
            for (int i = 0; i < 8; i++) dot += qs[g * FD_D + i * 32 + lane] * kk[i];
#pragma unroll
            for (int o = 16; o > 0; o >>= 1) dot += __shfl_xor_sync(0xffffffff, dot, o);
            float s = dot * scale;
            float mn = fmaxf(m[g], s);
            float corr = expf(m[g] - mn);
            float e = expf(s - mn);
            l[g] = l[g] * corr + e;
#pragma unroll
            for (int i = 0; i < 8; i++) acc[g][i] = acc[g][i] * corr + e * vv[i];
            m[g] = mn;
        }
    }
#pragma unroll
    for (int g = 0; g < G; g++) {
        float* out = part + (((size_t)kvh * P + p) * G + g) * (FD_D + 2);
#pragma unroll
        for (int i = 0; i < 8; i++) out[i * 32 + lane] = acc[g][i];
        if (lane == 0) {
            out[FD_D] = m[g];
            out[FD_D + 1] = l[g];
        }
    }
}

extern "C" __global__ void __launch_bounds__(256)
attn_decode_partial_g12(const float* q, const float* k, const float* v, const uint8_t* mask,
                        float* part, int Hkv, int kv_len, int chunk, float scale) {
    attn_decode_partial_impl<12>(q, k, v, mask, part, Hkv, kv_len, chunk, scale);
}

// Merges flash-decode partials: out[h, d] = sum_p e^(m_p - M) acc_p[d] / sum_p e^(m_p - M) l_p.
// One block per query head, one thread per d (blockDim.x == 256).
extern "C" __global__ void attn_decode_combine(const float* part, float* out, int H, int Hkv,
                                               int P) {
    int h = blockIdx.x, d = threadIdx.x;
    int G = H / Hkv;
    int kvh = h / G, g = h % G;
    float M = -INFINITY;
    for (int p = 0; p < P; p++) M = fmaxf(M, part[(((size_t)kvh * P + p) * G + g) * (FD_D + 2) + FD_D]);
    float num = 0.0f, den = 0.0f;
    for (int p = 0; p < P; p++) {
        const float* pr = part + (((size_t)kvh * P + p) * G + g) * (FD_D + 2);
        float mp = pr[FD_D];
        if (mp == -INFINITY) continue;
        float w = expf(mp - M);
        num += w * pr[d];
        den += w * pr[FD_D + 1];
    }
    out[(size_t)h * FD_D + d] = num / den;
}
