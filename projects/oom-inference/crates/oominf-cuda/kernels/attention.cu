// Rotary attention with block-sparse (QSA) key selection: prefill and flash-decode.
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

// Masked GQA attention for several query tokens, flash style: one block owns one KV
// head and TQ tokens x G query heads (the heads sharing that KV head), R = TQ * G <= 48
// rows. Key and value tiles of 16 rows are staged in shared memory once and read by
// every row, the online softmax keeps no score matrix, and a tile no row may see is
// skipped. Arithmetic is fp32 throughout (dots, expf, accumulation), as the reference.
// Grid (ceil(T / TQ), Hkv), block 256, dynamic shared memory ATTN_SMEM_BYTES.
// q: [T, H, D]; k, v caches: [kv_len_max, Hkv, D]; mask: [T, kv_stride]; out: [T, H, D].
#define ATTN_R 48
#define ATTN_KT 16
#define ATTN_STRIDE 260  // floats per Q/K row: 256 + 4 keeps float4 reads conflict-free
#define ATTN_SMEM_FLOATS \
    (ATTN_R * ATTN_STRIDE + ATTN_KT * ATTN_STRIDE + ATTN_KT * 256 + ATTN_R * ATTN_KT + 3 * ATTN_R)

extern "C" __global__ void __launch_bounds__(256)
attn_prefill(const float* q, const float* k, const float* v, const uint8_t* mask, float* out,
             int T, int H, int Hkv, int D, int kv_len, int kv_stride, float scale, int TQ) {
    extern __shared__ __align__(16) float smem[];
    float* qs = smem;                          // [R][STRIDE]
    float* ks = qs + ATTN_R * ATTN_STRIDE;     // [KT][STRIDE]
    float* vs = ks + ATTN_KT * ATTN_STRIDE;    // [KT][256]
    float* ps = vs + ATTN_KT * 256;            // [R][KT] scores, then probabilities
    float* rm = ps + ATTN_R * ATTN_KT;         // running max per row
    float* rl = rm + ATTN_R;                   // running sum per row
    float* rc = rl + ATTN_R;                   // this tile's rescale per row
    int G = H / Hkv, R = TQ * G;
    int t0 = blockIdx.x * TQ, kvh = blockIdx.y;
    int tid = threadIdx.x;
    // Row r is token t0 + r / G, head kvh * G + r % G.
    for (int i = tid; i < ATTN_R * (D / 4); i += 256) {
        int r = i / (D / 4), d = (i % (D / 4)) * 4;
        int t = t0 + r / G;
        float4 val = make_float4(0.f, 0.f, 0.f, 0.f);
        if (r < R && t < T)
            val = *reinterpret_cast<const float4*>(q + ((size_t)t * H + kvh * G + r % G) * D + d);
        *reinterpret_cast<float4*>(qs + r * ATTN_STRIDE + d) = val;
    }
    if (tid < ATTN_R) {
        rm[tid] = -INFINITY;
        rl[tid] = 0.0f;
    }
    // Scores: thread (rg, kk) dots rows 3 rg .. 3 rg + 2 with key kk of the tile.
    int kk = tid & 15, rg = tid >> 4;
    // Values: thread (rgo, dg) accumulates rows 12 rgo .. 12 rgo + 11, columns 4 dg .. 4 dg + 3.
    int dg = tid & 63, rgo = tid >> 6;
    float acc[12][4] = {};
    for (int j0 = 0; j0 < kv_len; j0 += ATTN_KT) {
        // Skip a tile no row may see (uniform across the block).
        bool any = false;
        if (tid < TQ * ATTN_KT) {
            int t = t0 + tid / ATTN_KT, j = j0 + tid % ATTN_KT;
            any = t < T && j < kv_len && mask[(size_t)t * kv_stride + j];
        }
        if (!__syncthreads_or(any)) continue;
        for (int i = tid; i < ATTN_KT * (D / 4); i += 256) {
            int key = i / (D / 4), d = (i % (D / 4)) * 4;
            int j = j0 + key;
            float4 kv4 = make_float4(0.f, 0.f, 0.f, 0.f), vv4 = kv4;
            if (j < kv_len) {
                size_t base = ((size_t)j * Hkv + kvh) * D + d;
                kv4 = *reinterpret_cast<const float4*>(k + base);
                vv4 = *reinterpret_cast<const float4*>(v + base);
            }
            *reinterpret_cast<float4*>(ks + key * ATTN_STRIDE + d) = kv4;
            *reinterpret_cast<float4*>(vs + key * 256 + d) = vv4;
        }
        __syncthreads();
        {
            float dot[3] = {0.f, 0.f, 0.f};
            const float* kr = ks + kk * ATTN_STRIDE;
            for (int d = 0; d < D; d += 4) {
                float4 kv4 = *reinterpret_cast<const float4*>(kr + d);
#pragma unroll
                for (int i = 0; i < 3; i++) {
                    float4 qv = *reinterpret_cast<const float4*>(qs + (rg * 3 + i) * ATTN_STRIDE + d);
                    dot[i] += qv.x * kv4.x;
                    dot[i] += qv.y * kv4.y;
                    dot[i] += qv.z * kv4.z;
                    dot[i] += qv.w * kv4.w;
                }
            }
            int j = j0 + kk;
#pragma unroll
            for (int i = 0; i < 3; i++) {
                int r = rg * 3 + i, t = t0 + r / G;
                bool ok = r < R && t < T && j < kv_len && mask[(size_t)t * kv_stride + j];
                ps[r * ATTN_KT + kk] = ok ? dot[i] * scale : -INFINITY;
            }
        }
        __syncthreads();
        if (tid < R) {
            float* pr = ps + tid * ATTN_KT;
            float mt = -INFINITY;
            for (int i = 0; i < ATTN_KT; i++) mt = fmaxf(mt, pr[i]);
            float mo = rm[tid], mn = fmaxf(mo, mt);
            if (mn == -INFINITY) {
                rc[tid] = 1.0f;
                for (int i = 0; i < ATTN_KT; i++) pr[i] = 0.0f;
            } else {
                float corr = expf(mo - mn), sum = 0.0f;
                for (int i = 0; i < ATTN_KT; i++) {
                    float e = pr[i] == -INFINITY ? 0.0f : expf(pr[i] - mn);
                    pr[i] = e;
                    sum += e;
                }
                rl[tid] = rl[tid] * corr + sum;
                rm[tid] = mn;
                rc[tid] = corr;
            }
        }
        __syncthreads();
        if (dg * 4 < D) {
#pragma unroll
            for (int i = 0; i < 12; i++) {
                float c = rc[rgo * 12 + i];
#pragma unroll
                for (int c4 = 0; c4 < 4; c4++) acc[i][c4] *= c;
            }
            for (int key = 0; key < ATTN_KT; key++) {
                float4 vv = *reinterpret_cast<const float4*>(vs + key * 256 + dg * 4);
#pragma unroll
                for (int i = 0; i < 12; i++) {
                    float p = ps[(rgo * 12 + i) * ATTN_KT + key];
                    acc[i][0] += p * vv.x;
                    acc[i][1] += p * vv.y;
                    acc[i][2] += p * vv.z;
                    acc[i][3] += p * vv.w;
                }
            }
        }
        __syncthreads();  // tiles and probabilities are rewritten by the next tile
    }
    if (dg * 4 >= D) return;
#pragma unroll
    for (int i = 0; i < 12; i++) {
        int r = rgo * 12 + i, t = t0 + r / G;
        if (r >= R || t >= T) continue;
        float l = rl[r];
        float4 o = make_float4(acc[i][0] / l, acc[i][1] / l, acc[i][2] / l, acc[i][3] / l);
        *reinterpret_cast<float4*>(out + ((size_t)t * H + kvh * G + r % G) * D + dg * 4) = o;
    }
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
