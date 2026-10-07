#include <metal_stdlib>
using namespace metal;

kernel void embedding(device const ushort* w [[buffer(0)]], device float* out [[buffer(1)]], constant uint* p [[buffer(2)]], uint i [[thread_position_in_grid]]) {
    out[i] = as_type<float>(uint(w[p[0]*p[1]+i]) << 16);
}

inline float bf16(ushort value) { return as_type<float>(uint(value) << 16); }
inline float e4m3(uchar value) {
    uint e = (value >> 3) & 15, m = value & 7;
    // Normal E4M3 values map directly into the fp32 exponent and mantissa.
    float mag = e == 0 ? float(m) / 512.0f : as_type<float>(((e+120)<<23) | (m<<20));
    if (e == 15 && m == 7) mag = NAN;
    return value & 128 ? -mag : mag;
}
constant float e2m1[16] = {0, .5, 1, 1.5, 2, 3, 4, 6, -0., -.5, -1, -1.5, -2, -3, -4, -6};
inline float sigmoid(float x) { return 1.0f/(1.0f+exp(-x)); }
inline float silu(float x) { return x*sigmoid(x); }

kernel void causal_conv(device const float* x [[buffer(0)]], device const ushort* w [[buffer(1)]], device float* history [[buffer(2)]], device float* out [[buffer(3)]], constant uint* p [[buffer(4)]], uint c [[thread_position_in_grid]]) {
    uint width = p[0], taps = p[1];
    float value = x[c]*bf16(w[c*taps+taps-1]);
    for (uint j=0; j+1<taps; ++j) value += history[j*width+c]*bf16(w[c*taps+j]);
    for (uint j=0; j+2<taps; ++j) history[j*width+c] = history[(j+1)*width+c];
    if (taps>1) history[(taps-2)*width+c] = x[c];
    out[c] = silu(value);
}

kernel void delta_step(device const float* qkv [[buffer(0)]], device const float* a [[buffer(1)]], device const float* b [[buffer(2)]], device const ushort* log_a [[buffer(3)]], device const ushort* bias [[buffer(4)]], device float* state [[buffer(5)]], device float* out [[buffer(6)]], constant uint* p [[buffer(7)]], uint gid [[thread_position_in_grid]], uint lane [[thread_index_in_simdgroup]]) {
    uint item=gid/32, hv=item/p[3], hk=hv/(p[1]/p[0]), dk=p[2], key_width=p[0]*dk;
    float dt=a[hv]+bf16(bias[hv]);
    float small=exp(-abs(dt)), rounded=1.0f+small;
    float logarithm=rounded==1.0f ? small : log(rounded)*(small/(rounded-1.0f));
    float decay=exp(-exp(bf16(log_a[hv]))*(max(dt,0.0f)+logarithm));
    float prediction=0;
    for(uint c=lane;c<dk;c+=32) prediction += state[item*dk+c]*decay*qkv[key_width+hk*dk+c];
    float correction=(qkv[2*key_width+item]-simd_sum(prediction))*sigmoid(b[hv]);
    float sum=0;
    for(uint c=lane;c<dk;c+=32) {
        float value=state[item*dk+c]*decay+qkv[key_width+hk*dk+c]*correction;
        state[item*dk+c]=value;
        sum += value*qkv[hk*dk+c];
    }
    float result=simd_sum(sum)*rsqrt(float(dk));
    if(lane==0) out[item]=result;
}

kernel void rope_half(device float* x [[buffer(0)]], constant uint* p [[buffer(1)]], uint i [[thread_position_in_grid]]) {
    uint half_dim=p[1]/2, c=i%half_dim, at=(i/half_dim)*p[0]+c;
    float angle=float(p[2])*pow(as_type<float>(p[3]),-2.0f*float(c)/float(p[1]));
    float co=cos(angle), si=sin(angle), a=x[at], b=x[at+half_dim];
    x[at]=a*co-b*si; x[at+half_dim]=b*co+a*si;
}

kernel void kv_append(device const float* x [[buffer(0)]], device float* cache [[buffer(1)]], constant uint* p [[buffer(2)]], uint i [[thread_position_in_grid]]) { cache[p[0]*p[1]+i]=x[i]; }

// One SIMD group owns a query head. Stable softmax scores are kept in workspace.
kernel void gqa_step(device const float* q [[buffer(0)]], device const float* k [[buffer(1)]], device const float* v [[buffer(2)]], device float* out [[buffer(3)]], device float* scores [[buffer(4)]], constant uint* p [[buffer(5)]], uint gid [[thread_position_in_grid]], uint lane [[thread_index_in_simdgroup]]) {
    uint h=gid/32, kh=h/(p[0]/p[1]), d=p[2], len=p[3];
    float maximum=-INFINITY;
    for(uint t=0;t<len;++t) {
        float sum=0;
        for(uint c=lane;c<d;c+=32) sum += q[h*d+c]*k[(t*p[1]+kh)*d+c];
        float value=simd_sum(sum)*rsqrt(float(d));
        maximum=max(maximum,value);
        if(lane==0) scores[h*len+t]=value;
    }
    threadgroup_barrier(mem_flags::mem_device);
    float denominator=0;
    for(uint t=lane;t<len;t+=32) { float s=exp(scores[h*len+t]-maximum); scores[h*len+t]=s; denominator+=s; }
    denominator=simd_sum(denominator);
    threadgroup_barrier(mem_flags::mem_device);
    for(uint c=lane;c<d;c+=32) {
        float value=0;
        for(uint t=0;t<len;++t) value += scores[h*len+t]*v[(t*p[1]+kh)*d+c];
        out[h*d+c]=value/denominator;
    }
}

kernel void scaled_add(device const float* x [[buffer(0)]], device float* y [[buffer(1)]], constant uint* p [[buffer(2)]], uint i [[thread_position_in_grid]]) { y[i] += x[i]*as_type<float>(p[1]); }
kernel void shared_gate(device float* x [[buffer(0)]], device const float* gate [[buffer(1)]], constant uint* p [[buffer(2)]], uint i [[thread_position_in_grid]]) { x[i] *= sigmoid(gate[0]); }

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

// Binding records directly avoids pointer indirection and keeps Metal residency
// and lifetime tracking for every selected cache allocation.
#define ROUTED_BUFFERS device const uchar* r0 [[buffer(0)]], device const uchar* r1 [[buffer(1)]], device const uchar* r2 [[buffer(2)]], device const uchar* r3 [[buffer(3)]], device const uchar* r4 [[buffer(4)]], device const uchar* r5 [[buffer(5)]], device const uchar* r6 [[buffer(6)]], device const uchar* r7 [[buffer(7)]], device const float* x [[buffer(8)]], device float* y [[buffer(9)]], constant uint* p [[buffer(10)]], uint gid [[thread_position_in_grid]], uint lane [[thread_index_in_simdgroup]]

kernel void routed_gate_up(ROUTED_BUFFERS) {
    uint output=gid/32, row=output%p[2], expert=output/p[2], k=p[1];
    device const uchar* records[8]={r0,r1,r2,r3,r4,r5,r6,r7};
    device const uchar* record=records[expert];
    float gate=0, up=0;
    for(uint c=lane;c<k;c+=32) {
        uint at=row*k+c;
        uchar g=record[p[3]+at/2], u=record[p[5]+at/2];
        float value=x[c];
        gate += value*e2m1[(g>>((c&1)*4))&15]*e4m3(record[p[4]+row*(k/16)+c/16]);
        up += value*e2m1[(u>>((c&1)*4))&15]*e4m3(record[p[6]+row*(k/16)+c/16]);
    }
    gate=simd_sum(gate)*as_type<float>(p[9+expert*4]);
    up=simd_sum(up)*as_type<float>(p[10+expert*4]);
    if(lane==0) y[output]=silu(gate)*up;
}

kernel void routed_down(ROUTED_BUFFERS) {
    uint output=gid/32, row=output%p[1], expert=output/p[1], k=p[2];
    device const uchar* records[8]={r0,r1,r2,r3,r4,r5,r6,r7};
    device const uchar* record=records[expert];
    float acc=0;
    for(uint c=lane;c<k;c+=32) {
        uchar packed=record[p[7]+(row*k+c)/2];
        acc += x[expert*k+c]*e2m1[(packed>>((c&1)*4))&15]*e4m3(record[p[8]+row*(k/16)+c/16]);
    }
    float value=simd_sum(acc)*as_type<float>(p[11+expert*4]);
    if(lane==0) y[output]=value;
}

kernel void routed_mix(device const float* x [[buffer(0)]], device float* out [[buffer(1)]], constant uint* p [[buffer(2)]], uint c [[thread_position_in_grid]]) {
    float value=out[c];
    for(uint expert=0;expert<p[0];++expert) value += x[expert*p[1]+c]*as_type<float>(p[12+expert*4]);
    out[c]=value;
}
