#include <metal_stdlib>
using namespace metal;

inline float bf16(ushort value) { return as_type<float>(uint(value) << 16); }
inline float e4m3(uchar value) {
    uint e = (value >> 3) & 15, m = value & 7;
    float mag = e == 0 ? float(m) / 512.0f : ldexp(1.0f + float(m)/8.0f, int(e)-7);
    if (e == 15 && m == 7) mag = NAN;
    return value & 128 ? -mag : mag;
}
constant float e2m1[16] = {0, .5, 1, 1.5, 2, 3, 4, 6, -0., -.5, -1, -1.5, -2, -3, -4, -6};
inline float sigmoid(float x) { return 1.0f/(1.0f+exp(-x)); }
inline float silu(float x) { return x*sigmoid(x); }

kernel void elementwise(device const float* a [[buffer(0)]], device const float* b [[buffer(1)]],
    device float* out [[buffer(2)]], constant uint* p [[buffer(3)]], uint i [[thread_position_in_grid]]) {
    if (i >= p[1]) return;
    switch(p[0]) {
        case 0: out[i] = a[i]+b[i]; break;
        case 1: out[i] = silu(a[i]*as_type<float>(p[3])); break;
        case 2: out[i] = silu(a[i])*b[i]; break;
        case 3: { uint j = (i/p[2])*(2*p[2])+i%p[2]; out[i] = silu(a[j])*a[j+p[2]]; break; }
        case 4: out[i] = a[i]*sigmoid(b[i]); break;
    }
}

kernel void copy_columns(device const float* src [[buffer(0)]], device float* dst [[buffer(1)]],
    constant uint* p [[buffer(2)]], uint i [[thread_position_in_grid]]) {
    uint at = (i/p[4])*p[2]+p[3]+i%p[4];
    if (p[0]) dst[at] = src[i]; else dst[i] = src[at];
}

kernel void rmsnorm(device const float* x [[buffer(0)]], device const ushort* w [[buffer(1)]],
    device float* out [[buffer(2)]], constant uint* p [[buffer(3)]],
    uint gid [[thread_position_in_grid]], uint lane [[thread_index_in_simdgroup]]) {
    uint group = gid/32, d = p[0]; float sum = 0;
    for (uint c = lane; c < d; c += 32) { float value = x[group*d+c]; sum += value*value; }
    float inv = rsqrt(simd_sum(sum)/float(d)+as_type<float>(p[1]));
    for (uint c = lane; c < d; c += 32) out[group*d+c] = x[group*d+c]*inv*(bf16(w[c])+as_type<float>(p[2]));
}

kernel void l2norm(device float* x [[buffer(0)]], constant uint* p [[buffer(1)]],
    uint gid [[thread_position_in_grid]], uint lane [[thread_index_in_simdgroup]]) {
    uint head = gid/32, d = p[3], at = (head/p[2])*p[0]+p[1]+(head%p[2])*d;
    float sum = 0;
    for (uint c = lane; c < d; c += 32) { float value = x[at+c]; sum += value*value; }
    float inv = rsqrt(simd_sum(sum)+as_type<float>(p[4]));
    for (uint c = lane; c < d; c += 32) x[at+c] *= inv;
}

kernel void gated_norm(device const float* x [[buffer(0)]], device const float* z [[buffer(1)]],
    device const ushort* w [[buffer(2)]], device float* out [[buffer(3)]], constant uint* p [[buffer(4)]],
    uint gid [[thread_position_in_grid]], uint lane [[thread_index_in_simdgroup]]) {
    uint row = gid/32, d = p[3], z_at = p[0]+(row/p[2])*p[1]+(row%p[2])*d;
    float sum = 0;
    for (uint c = lane; c < d; c += 32) { float value = x[row*d+c]; sum += value*value; }
    float inv = rsqrt(simd_sum(sum)/float(d)+as_type<float>(p[4]));
    for (uint c = lane; c < d; c += 32) out[row*d+c] = x[row*d+c]*inv*bf16(w[c])*sigmoid(z[z_at+c]);
}

kernel void gemm_bf16(device const float* x [[buffer(0)]], device const ushort* w [[buffer(1)]],
    device float* y [[buffer(2)]], constant uint* p [[buffer(3)]], uint gid [[thread_position_in_grid]],
    uint lane [[thread_index_in_simdgroup]]) {
    uint output = gid / 32, row = output % p[1], token = output / p[1], k = p[2];
    float acc = 0;
    for (uint c = lane; c < k; c += 32) acc += x[token*k+c] * bf16(w[row*k+c]);
    float sum = simd_sum(acc);
    if (lane == 0) y[output] = sum;
}

kernel void gemm_fp8(device const float* x [[buffer(0)]], device const uchar* w [[buffer(1)]],
    device const float* scale [[buffer(2)]], device float* y [[buffer(3)]],
    constant uint* p [[buffer(4)]], uint gid [[thread_position_in_grid]], uint lane [[thread_index_in_simdgroup]]) {
    uint output = gid / 32, row = output % p[1], token = output / p[1], k = p[2];
    float acc = 0;
    for (uint c = lane; c < k; c += 32) acc += x[token*k+c] * e4m3(w[row*k+c]) * scale[row*((k+127)/128)+c/128];
    float sum = simd_sum(acc);
    if (lane == 0) y[output] = sum;
}

kernel void gemm_nvfp4(device const float* x [[buffer(0)]], device const uchar* w [[buffer(1)]],
    device const uchar* scale [[buffer(2)]], device float* y [[buffer(3)]],
    constant uint* p [[buffer(4)]], uint gid [[thread_position_in_grid]], uint lane [[thread_index_in_simdgroup]]) {
    uint output = gid / 32, row = output % p[1], token = output / p[1], k = p[2];
    float acc = 0;
    for (uint c = lane; c < k; c += 32) {
        uchar packed = w[(row*k+c)/2];
        uint code = (packed >> ((c & 1)*4)) & 15;
        acc += x[token*k+c] * e2m1[code] * e4m3(scale[row*(k/16)+c/16]);
    }
    float sum = simd_sum(acc) * as_type<float>(p[3]);
    if (lane == 0) y[output] = sum;
}
