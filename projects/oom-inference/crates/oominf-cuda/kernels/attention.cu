// Rotary attention with block-sparse (QSA) key selection: prefill and flash-decode.
// All arithmetic is fp32.

#include <cuda_fp16.h>
#include <math.h>
#include <stdint.h>

// KV cache rows (KvFormat). bits == 0: D fp32 values. bits 3 or 4 (Turbo): the head
// vector was rotated by kv_rotate_forward and is stored as D/32 blocks of 32
// coordinates: an fp16 scale per block (the block's RMS; the scales padded to 16 bytes),
// then per block `bits` 32-bit words, word j holding bit j of the 32 coordinates'
// codebook indices (bit-planes, so lane l of a warp reads bit l). A coordinate is
// level[idx] * scale, with Lloyd-Max levels for a unit Gaussian (KvFormat::codebooks:
// the levels for b bits start at 2^b - 4).
__device__ __forceinline__ int kv_scale_bytes(int D) { return ((D / 32) * 2 + 15) & ~15; }

__device__ __forceinline__ size_t kv_row_bytes(int D, int bits) {
    return bits ? (size_t)kv_scale_bytes(D) + (size_t)(D / 32) * bits * 4 : (size_t)D * 4;
}

// Copies the `bits` codebook into shared `lv` (the caller synchronises before use).
__device__ __forceinline__ void kv_levels_load(float* lv, const float* levels, int bits) {
    if (!bits) return;
    for (int i = threadIdx.x; i < (1 << bits); i += blockDim.x) lv[i] = levels[(1 << bits) - 4 + i];
}

// Coordinate d of a cache row (rotated space for Turbo rows); `lv` is its codebook.
__device__ __forceinline__ float kv_value(const uint8_t* row, int d, int D, int bits,
                                          const float* lv) {
    if (!bits) return reinterpret_cast<const float*>(row)[d];
    int b = d >> 5, lane = d & 31;
    float sc = __half2float(reinterpret_cast<const __half*>(row)[b]);
    const uint32_t* w = reinterpret_cast<const uint32_t*>(row + kv_scale_bytes(D)) + b * bits;
    int idx = 0;
    for (int j = 0; j < bits; j++) idx |= ((w[j] >> lane) & 1) << j;
    return lv[idx] * sc;
}

// The fixed random sign of coordinate i in the rotation.
__device__ __forceinline__ float kv_sign(int i) {
    return (((unsigned)i * 2654435761u) >> 31) ? -1.0f : 1.0f;
}

// Unnormalised Walsh-Hadamard transform of s[0..D) in shared memory (D a power of two,
// blockDim >= D); synchronises before and after.
__device__ void kv_wht(float* s, int D) {
    for (int h = 1; h < D; h <<= 1) {
        __syncthreads();
        int i = threadIdx.x;
        if (i < D && (i & h) == 0) {
            float a = s[i], b = s[i + h];
            s[i] = a + b;
            s[i + h] = a - b;
        }
    }
    __syncthreads();
}

// Rotates rows of D coordinates in place: forward y = H (s . x) / sqrt(D), inverse
// x = s . (H y) / sqrt(D) (H symmetric, H H = D I). One block of D threads per row.
extern "C" __global__ void kv_rotate(float* x, int D, int inverse) {
    __shared__ float s[256];
    float* r = x + (size_t)blockIdx.x * D;
    int i = threadIdx.x;
    float norm = 1.0f / sqrtf((float)D);
    s[i] = inverse ? r[i] : r[i] * kv_sign(i);
    kv_wht(s, D);
    r[i] = inverse ? s[i] * kv_sign(i) * norm : s[i] * norm;
}

// Appends rows of `src` ([n, D], cache order) to the cache at row `row0`, rotating and
// quantising them for bits 3 or 4. One block of D threads (a multiple of 32) per row.
extern "C" __global__ void kv_append(const float* src, uint8_t* cache, long long row0, int D,
                                     int bits, const float* levels) {
    __shared__ float s[256];
    __shared__ float lv[256];
    size_t r = blockIdx.x;
    int i = threadIdx.x;
    const float* x = src + r * D;
    uint8_t* row = cache + ((size_t)row0 + r) * kv_row_bytes(D, bits);
    if (!bits) {
        reinterpret_cast<float*>(row)[i] = x[i];
        return;
    }
    kv_levels_load(lv, levels, bits);
    s[i] = x[i] * kv_sign(i);
    kv_wht(s, D);
    float y = s[i] / sqrtf((float)D);
    float ss = y * y;
    for (int o = 16; o > 0; o >>= 1) ss += __shfl_xor_sync(0xffffffff, ss, o);
    __half hs = __float2half_rn(sqrtf(ss / 32.0f));
    float sc = __half2float(hs);
    float u = sc > 0.0f ? y / sc : 0.0f;
    // Nearest level: binary search the ascending codebook, then the closer neighbour.
    int lo = 0, hi = (1 << bits) - 1;
    while (hi - lo > 1) {
        int mid = (lo + hi) >> 1;
        if (lv[mid] <= u) lo = mid;
        else hi = mid;
    }
    int idx = fabsf(u - lv[lo]) <= fabsf(u - lv[hi]) ? lo : hi;
    int lane = i & 31, b = i >> 5;
    if (lane == 0) reinterpret_cast<__half*>(row)[b] = hs;
    uint32_t* w = reinterpret_cast<uint32_t*>(row + kv_scale_bytes(D)) + b * bits;
    for (int j = 0; j < bits; j++) {
        unsigned m = __ballot_sync(0xffffffff, (idx >> j) & 1);
        if (lane == j) w[j] = m;
    }
}

// Reads cache rows back as fp32 [n, D] (decoded and un-rotated for bits 3 or 4). One
// block of D threads per row.
extern "C" __global__ void kv_read(const uint8_t* cache, float* dst, int D, int bits,
                                   const float* levels) {
    __shared__ float s[256];
    __shared__ float lv[256];
    size_t r = blockIdx.x;
    int i = threadIdx.x;
    const uint8_t* row = cache + r * kv_row_bytes(D, bits);
    float* out = dst + r * D;
    if (!bits) {
        out[i] = reinterpret_cast<const float*>(row)[i];
        return;
    }
    kv_levels_load(lv, levels, bits);
    __syncthreads();
    s[i] = kv_value(row, i, D, bits, lv);
    kv_wht(s, D);
    out[i] = s[i] * kv_sign(i) / sqrtf((float)D);
}

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
// Tiled: a CUDA block stages QSA_TQ queries and QSA_TB key blocks in shared memory and
// each thread scores one key block for 4 queries; every score is summed in the same
// order as a plain loop over h then d. Grid (ceil(nb_max / QSA_TB), ceil(T / QSA_TQ)),
// block 256, dynamic shared memory QSA_SMEM_FLOATS * 4 bytes. nheads * Di <= 512,
// Di <= 128.
#define QSA_TQ 16
#define QSA_TB 64
#define QSA_KSTRIDE 129  // odd stride: threads reading different key blocks hit distinct banks
#define QSA_SMEM_FLOATS (QSA_TQ * 512 + QSA_TB * QSA_KSTRIDE)

extern "C" __global__ void __launch_bounds__(256)
qsa_scores(const float* q, const float* kb, float* scores, int T, int start, int nheads, int Di,
           int ratio, int kv_stride) {
    extern __shared__ float qsa_smem[];
    float* qs = qsa_smem;                  // [TQ][nheads * Di]
    float* ks = qs + QSA_TQ * 512;         // [TB][KSTRIDE]
    int b0 = blockIdx.x * QSA_TB, t0 = blockIdx.y * QSA_TQ;
    int tlast = min(t0 + QSA_TQ, T) - 1;
    int nb_last = (start + tlast + 1) / ratio;
    if (b0 >= nb_last) return;  // no query of this tile sees these blocks
    int tid = threadIdx.x, qd = nheads * Di;
    for (int i = tid; i < QSA_TQ * qd; i += 256) {
        int tq = i / qd, e = i % qd;
        qs[tq * 512 + e] = t0 + tq < T ? q[(size_t)(t0 + tq) * qd + e] : 0.0f;
    }
    for (int i = tid; i < QSA_TB * Di; i += 256) {
        int bb = i / Di, d = i % Di;
        ks[bb * QSA_KSTRIDE + d] = b0 + bb < nb_last ? kb[(size_t)(b0 + bb) * Di + d] : 0.0f;
    }
    __syncthreads();
    int bb = tid % QSA_TB, tg = tid / QSA_TB;  // key block, group of 4 queries
    int b = b0 + bb;
    const float* kr = ks + bb * QSA_KSTRIDE;
    float sum[4] = {0.f, 0.f, 0.f, 0.f};
    for (int h = 0; h < nheads; h++) {
        float dot[4] = {0.f, 0.f, 0.f, 0.f};
        for (int d = 0; d < Di; d++) {
            float kv = kr[d];
#pragma unroll
            for (int i = 0; i < 4; i++) dot[i] += qs[(tg * 4 + i) * 512 + h * Di + d] * kv;
        }
#pragma unroll
        for (int i = 0; i < 4; i++) sum[i] += fmaxf(dot[i], 0.0f);
    }
    float div = sqrtf((float)Di);
#pragma unroll
    for (int i = 0; i < 4; i++) {
        int t = t0 + tg * 4 + i;
        if (t < T && b < (start + t + 1) / ratio)
            scores[(size_t)t * (kv_stride / ratio + 1) + b] = sum[i] / div;
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
attn_prefill(const float* q, const uint8_t* k, const uint8_t* v, const uint8_t* mask, float* out,
             int T, int H, int Hkv, int D, int kv_len, int kv_stride, float scale, int TQ, int kb,
             int vb, const float* levels) {
    extern __shared__ __align__(16) float smem[];
    __shared__ float lvk[256], lvv[256];
    kv_levels_load(lvk, levels, kb);
    kv_levels_load(lvv, levels, vb);
    size_t krow = kv_row_bytes(D, kb), vrow = kv_row_bytes(D, vb);
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
                size_t r = (size_t)j * Hkv + kvh;
                const uint8_t* kr = k + r * krow;
                const uint8_t* vr = v + r * vrow;
                kv4 = kb ? make_float4(kv_value(kr, d, D, kb, lvk), kv_value(kr, d + 1, D, kb, lvk),
                                       kv_value(kr, d + 2, D, kb, lvk),
                                       kv_value(kr, d + 3, D, kb, lvk))
                         : *reinterpret_cast<const float4*>(kr + 4 * d);
                vv4 = vb ? make_float4(kv_value(vr, d, D, vb, lvv), kv_value(vr, d + 1, D, vb, lvv),
                                       kv_value(vr, d + 2, D, vb, lvv),
                                       kv_value(vr, d + 3, D, vb, lvv))
                         : *reinterpret_cast<const float4*>(vr + 4 * d);
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
__device__ void attn_decode_partial_impl(const float* q, const uint8_t* k, const uint8_t* v,
                                         const uint8_t* mask, float* part, int Hkv,
                                         int kv_len, int chunk, float scale, int kb, int vb,
                                         const float* levels) {
    __shared__ float qs[G * FD_D];
    __shared__ float lvk[256], lvv[256];
    kv_levels_load(lvk, levels, kb);
    kv_levels_load(lvv, levels, vb);
    size_t krow = kv_row_bytes(FD_D, kb), vrow = kv_row_bytes(FD_D, vb);
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
        const uint8_t* kr = k + ((size_t)j * Hkv + kvh) * krow;
        const uint8_t* vr = v + ((size_t)j * Hkv + kvh) * vrow;
        float kk[8], vv[8];
#pragma unroll
        for (int i = 0; i < 8; i++) {
            kk[i] = kv_value(kr, i * 32 + lane, FD_D, kb, lvk);
            vv[i] = kv_value(vr, i * 32 + lane, FD_D, vb, lvv);
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
attn_decode_partial_g12(const float* q, const uint8_t* k, const uint8_t* v, const uint8_t* mask,
                        float* part, int Hkv, int kv_len, int chunk, float scale, int kb, int vb,
                        const float* levels) {
    attn_decode_partial_impl<12>(q, k, v, mask, part, Hkv, kv_len, chunk, scale, kb, vb, levels);
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
