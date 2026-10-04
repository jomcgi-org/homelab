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
// Block b covers key positions [b*ratio, (b+1)*ratio); a query sees nb = (p+1)/ratio
// complete blocks. score[b] = sum_h relu(q[t,h] . kb[b]) / sqrt(Di). The top `topk`
// blocks (ties to the lower block index) plus the incomplete tail are selected.
// mask: [T, kv_stride] bytes, 1 where key position j is attendable (j <= p).
extern "C" __global__ void qsa_select(const float* q, const float* kb, float* scores,
                                      uint8_t* mask, int T, int start, int nheads, int Di,
                                      int ratio, int topk, int kv_stride) {
    int t = blockIdx.x;
    int p = start + t;
    int nb = (p + 1) / ratio;
    float* sc = scores + (size_t)t * (kv_stride / ratio + 1);
    const float* qt = q + (size_t)t * nheads * Di;
    float inv = rsqrtf((float)Di);
    for (int b = threadIdx.x; b < nb; b += blockDim.x) {
        float s = 0.0f;
        for (int h = 0; h < nheads; h++) {
            float dot = 0.0f;
            for (int d = 0; d < Di; d++) dot += qt[h * Di + d] * kb[(size_t)b * Di + d];
            s += fmaxf(dot, 0.0f);
        }
        sc[b] = s * inv;
    }
    __syncthreads();
    uint8_t* m = mask + (size_t)t * kv_stride;
    for (int j = threadIdx.x; j < kv_stride; j += blockDim.x) {
        uint8_t keep = 0;
        if (j <= p) {
            int b = j / ratio;
            if (b >= nb || nb <= topk) {
                keep = 1;  // tail token, or every block fits the budget
            } else {
                float s = sc[b];
                int rank = 0;
                for (int o = 0; o < nb && rank < topk; o++)
                    rank += (sc[o] > s) || (sc[o] == s && o < b);
                keep = rank < topk;
            }
        }
        m[j] = keep;
    }
}

// Masked GQA attention, one block (256 threads) per (query t, head h).
// q: [T, H, D] (already scaled is NOT assumed; `scale` applied here). k, v caches:
// [kv_len_max, Hkv, D]. mask: [T, kv_stride]. scores: scratch [T, H, kv_stride].
// out: [T, H, D].
extern "C" __global__ void attn_masked(const float* q, const float* k, const float* v,
                                       const uint8_t* mask, float* scores, float* out, int T,
                                       int H, int Hkv, int D, int kv_len, int kv_stride,
                                       float scale) {
    int t = blockIdx.x / H, h = blockIdx.x % H;
    int kvh = h / (H / Hkv);
    const float* qr = q + ((size_t)t * H + h) * D;
    const uint8_t* m = mask + (size_t)t * kv_stride;
    float* sc = scores + ((size_t)t * H + h) * kv_stride;
    __shared__ float red[256];

    float mx = -INFINITY;
    for (int j = threadIdx.x; j < kv_len; j += blockDim.x) {
        float s = -INFINITY;
        if (m[j]) {
            const float* kr = k + ((size_t)j * Hkv + kvh) * D;
            float dot = 0.0f;
            for (int d = 0; d < D; d++) dot += qr[d] * kr[d];
            s = dot * scale;
        }
        sc[j] = s;
        mx = fmaxf(mx, s);
    }
    red[threadIdx.x] = mx;
    __syncthreads();
    for (int s = blockDim.x / 2; s > 0; s >>= 1) {
        if (threadIdx.x < s) red[threadIdx.x] = fmaxf(red[threadIdx.x], red[threadIdx.x + s]);
        __syncthreads();
    }
    mx = red[0];
    __syncthreads();
    float sum = 0.0f;
    for (int j = threadIdx.x; j < kv_len; j += blockDim.x) {
        float e = m[j] ? expf(sc[j] - mx) : 0.0f;
        sc[j] = e;
        sum += e;
    }
    red[threadIdx.x] = sum;
    __syncthreads();
    for (int s = blockDim.x / 2; s > 0; s >>= 1) {
        if (threadIdx.x < s) red[threadIdx.x] += red[threadIdx.x + s];
        __syncthreads();
    }
    float inv = 1.0f / red[0];
    for (int d = threadIdx.x; d < D; d += blockDim.x) {
        float acc = 0.0f;
        for (int j = 0; j < kv_len; j++) {
            float p = sc[j];
            if (p != 0.0f) acc += p * v[((size_t)j * Hkv + kvh) * D + d];
        }
        out[((size_t)t * H + h) * D + d] = acc * inv;
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
