//! The device a model runs on, as the operations models need from it.
//!
//! A platform implements every trait here (most through device kernels); models
//! and expert tiers are written against [`Backend`], which bundles them. Shapes are
//! row-major element counts; buffers hold at least the elements an operation
//! touches. Work is ordered: each operation sees the effects of the operations
//! issued before it, and host reads (`download_*`) wait for them.

use anyhow::Result;

/// A device buffer of elements of one type.
pub trait DeviceBuffer {
    /// Elements the buffer holds.
    fn len(&self) -> usize;
    fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

/// `len` elements of `buf` starting at element `start`.
pub struct View<'a, T> {
    pub buf: &'a T,
    pub start: usize,
    pub len: usize,
}

impl<'a, T: DeviceBuffer> View<'a, T> {
    pub fn new(buf: &'a T, start: usize, len: usize) -> Self {
        debug_assert!(start + len <= buf.len(), "view out of bounds");
        View { buf, start, len }
    }
}

/// Device memory: allocation, host transfers and raw addresses.
pub trait Memory {
    /// fp32 activations and state.
    type F32: DeviceBuffer;
    /// bf16 values stored as raw `u16` words (dense weights, GEMM operands).
    type Bf16: DeviceBuffer;
    /// Raw bytes (quantised records, masks).
    type Bytes: DeviceBuffer;
    type I32: DeviceBuffer;
    type U64: DeviceBuffer;

    /// fp32 buffer of `n` elements whose contents are unspecified (for outputs an
    /// operation fully writes).
    fn uninit(&self, n: usize) -> Result<Self::F32>;
    fn zeros(&self, n: usize) -> Result<Self::F32>;
    fn fill_zero(&self, buf: &mut Self::F32) -> Result<()>;
    fn upload_f32(&self, host: &[f32]) -> Result<Self::F32>;
    /// Copies `host` into `dst` (same length).
    fn upload_into(&self, host: &[f32], dst: &mut Self::F32) -> Result<()>;
    fn download_f32(&self, buf: &Self::F32) -> Result<Vec<f32>>;
    /// Copies `host` into `dst` starting at element `offset`, ordered after the
    /// compute issued so far.
    fn write_f32_at(&self, host: &[f32], dst: &mut Self::F32, offset: usize) -> Result<()>;

    fn upload_bf16(&self, host: &[u16]) -> Result<Self::Bf16>;
    /// bf16 buffer of `n` elements with unspecified contents.
    fn uninit_bf16(&self, n: usize) -> Result<Self::Bf16>;

    fn upload_bytes(&self, host: &[u8]) -> Result<Self::Bytes>;
    fn uninit_bytes(&self, n: usize) -> Result<Self::Bytes>;
    fn zeros_bytes(&self, n: usize) -> Result<Self::Bytes>;
    /// Like [`Memory::zeros_bytes`], but kept in host memory that the device reads
    /// and writes in place over the bus (no device memory used): for large buffers
    /// read sparsely, such as a decode-time KV cache.
    fn zeros_bytes_host(&self, n: usize) -> Result<Self::Bytes> {
        self.zeros_bytes(n)
    }
    fn download_bytes(&self, buf: &Self::Bytes) -> Result<Vec<u8>>;
    /// `dst[dst_off .. dst_off + n] = src[src_off .. src_off + n]` (bytes), ordered
    /// after the compute issued so far.
    fn copy_bytes(
        &self,
        src: &Self::Bytes,
        src_off: usize,
        dst: &mut Self::Bytes,
        dst_off: usize,
        n: usize,
    ) -> Result<()>;

    fn upload_i32(&self, host: &[i32]) -> Result<Self::I32>;
    fn download_i32(&self, buf: &Self::I32) -> Result<Vec<i32>>;
    /// Copies `host` into the front of `dst`, reallocating `dst` larger if needed.
    fn write_i32(&self, host: &[i32], dst: &mut Self::I32) -> Result<()>;

    fn zeros_u64(&self, n: usize) -> Result<Self::U64>;
    /// Copies `host` into the front of `dst`, reallocating `dst` larger if needed.
    fn write_u64(&self, host: &[u64], dst: &mut Self::U64) -> Result<()>;

    /// Device address of a byte buffer (valid while it lives).
    fn bytes_addr(&self, buf: &Self::Bytes) -> u64;

    /// Waits until all issued work has completed.
    fn sync(&self) -> Result<()>;
    /// `(free, total)` device memory in bytes.
    fn mem_info(&self) -> Result<(usize, usize)>;
}

/// Matrix products with fp32 accumulation.
/// How dense (non-expert) weight matrices are stored on the device.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DenseFormat {
    /// As the checkpoint stores them (exact).
    Bf16,
    /// FP8 e4m3 with one f32 scale per 128 weights of a row (half the bytes; lossy).
    Fp8,
}

impl DenseFormat {
    pub fn parse(s: &str) -> std::result::Result<Self, String> {
        match s {
            "bf16" => Ok(DenseFormat::Bf16),
            "fp8" => Ok(DenseFormat::Fp8),
            other => Err(format!("unknown dense format {other:?} (bf16, fp8)")),
        }
    }
}

/// A dense weight matrix `[n, k]` on the device, in its [`DenseFormat`].
pub enum Weight<B: Memory> {
    Bf16(B::Bf16),
    /// e4m3 bytes `[n, k]` and one scale per [`crate::fp8::BLOCK`] weights of a row:
    /// `w[r, c] = scale[r * k.div_ceil(BLOCK) + c / BLOCK] * q[r, c]`.
    Fp8 {
        q: B::Bytes,
        scale: B::F32,
    },
}

pub trait Linear: Memory {
    /// `y[t, n] = x[t, k] @ w[n, k]^T` for a [`Weight`] in either format.
    #[allow(clippy::too_many_arguments)]
    fn gemm_w(
        &self,
        x: &Self::F32,
        w: &Weight<Self>,
        y: &mut Self::F32,
        scratch: &mut Self::Bf16,
        t: usize,
        n: usize,
        k: usize,
    ) -> Result<()>
    where
        Self: Sized,
    {
        match w {
            Weight::Bf16(w) => self.gemm_bf16(x, w, y, scratch, t, n, k),
            Weight::Fp8 { q, scale } => self.gemm_fp8(x, q, scale, y, scratch, t, n, k),
        }
    }

    /// As [`Linear::gemm_bf16`] with FP8 e4m3 weights `q` scaled per block by
    /// `scale` (see [`Weight::Fp8`]).
    /// Larger `t` may dequantize the weights to bf16 into a cache the backend keeps
    /// until [`Linear::release_weight_cache`].
    #[allow(clippy::too_many_arguments)]
    fn gemm_fp8(
        &self,
        x: &Self::F32,
        q: &Self::Bytes,
        scale: &Self::F32,
        y: &mut Self::F32,
        scratch: &mut Self::Bf16,
        t: usize,
        n: usize,
        k: usize,
    ) -> Result<()>;

    /// Frees dequantized weights kept for [`Linear::gemm_fp8`] (e.g. when a
    /// prefill ends and the memory goes back to the expert tier). The cache is keyed
    /// by the weights' device address: call this before freeing FP8 weights.
    fn release_weight_cache(&self) {}

    /// `y[t, n] = x[t, k] @ w[n, k]^T` with bf16 weights and fp32 output. Small `t`
    /// reads the fp32 activations directly; larger `t` may round them to bf16 into
    /// `scratch` (at least `t * k` elements) for tensor cores.
    #[allow(clippy::too_many_arguments)]
    fn gemm_bf16(
        &self,
        x: &Self::F32,
        w: &Self::Bf16,
        y: &mut Self::F32,
        scratch: &mut Self::Bf16,
        t: usize,
        n: usize,
        k: usize,
    ) -> Result<()>;
}

/// Elementwise operations and data movement.
pub trait Elementwise: Memory {
    /// `out = x + y`.
    fn add(&self, x: &Self::F32, y: &Self::F32, out: &mut Self::F32, n: usize) -> Result<()>;
    /// `y = silu(x * scale)`.
    fn silu_scale(&self, x: &Self::F32, y: &mut Self::F32, scale: f32, n: usize) -> Result<()>;
    /// `y = silu(gate) * up`.
    fn silu_mul(&self, gate: &Self::F32, up: &Self::F32, y: &mut Self::F32, n: usize)
    -> Result<()>;
    /// SwiGLU on stacked rows: `y[r, i] = silu(gu[r, i]) * gu[r, n + i]` for `gu`
    /// `[rows, 2n]` (gate then up, as one GEMM over stacked weights writes them).
    fn silu_mul_rows(&self, gu: &Self::F32, y: &mut Self::F32, rows: usize, n: usize)
    -> Result<()>;
    /// `x *= sigmoid(gate)`.
    fn mul_sigmoid(&self, x: &mut Self::F32, gate: &Self::F32, n: usize) -> Result<()>;
    /// Rows `[first, first + n)` of a `[_, h]` matrix into `dst`.
    fn copy_rows(
        &self,
        src: &Self::F32,
        first: usize,
        n: usize,
        h: usize,
        dst: &mut Self::F32,
    ) -> Result<()>;
    /// `dst[rows, cols] = src[:, col .. col + cols]` for `src` rows of `stride`.
    fn copy_cols(
        &self,
        src: &Self::F32,
        dst: &mut Self::F32,
        rows: usize,
        stride: usize,
        col: usize,
        cols: usize,
    ) -> Result<()>;
    /// `dst[r, col .. col + cols] = src[r, :]` for `dst` rows of `stride`.
    fn put_cols(
        &self,
        src: &Self::F32,
        dst: &mut Self::F32,
        rows: usize,
        stride: usize,
        col: usize,
        cols: usize,
    ) -> Result<()>;
    /// `dst[offset .. offset + n] = src[..n]`.
    fn copy_at(&self, src: &Self::F32, dst: &mut Self::F32, offset: usize, n: usize) -> Result<()>;
    /// `dst[dst_off .. dst_off + n] = src[src_off .. src_off + n]`.
    fn copy_range(
        &self,
        src: &Self::F32,
        src_off: usize,
        dst: &mut Self::F32,
        dst_off: usize,
        n: usize,
    ) -> Result<()>;
    /// `dst[b, a, :] = src[a, b, :]` for `src` `[a_len, b_len, d]`.
    fn swap01(
        &self,
        src: &Self::F32,
        dst: &mut Self::F32,
        a_len: usize,
        b_len: usize,
        d: usize,
    ) -> Result<()>;
    /// Mean of each group of `ratio` consecutive rows of width `d`.
    fn pool_rows(
        &self,
        raw: &Self::F32,
        out: &mut Self::F32,
        nblocks: usize,
        ratio: usize,
        d: usize,
    ) -> Result<()>;
}

/// Normalisations, all computed in fp32.
pub trait Norm: Memory {
    /// RMSNorm over groups of `group` elements within rows of `row` elements;
    /// `plus_one` is 1.0 for `(1 + w)` weights and 0.0 for plain `w`.
    #[allow(clippy::too_many_arguments)]
    fn rmsnorm_groups(
        &self,
        x: &Self::F32,
        w: &Self::Bf16,
        out: &mut Self::F32,
        rows: usize,
        row: usize,
        group: usize,
        eps: f32,
        plus_one: f32,
    ) -> Result<()>;
    /// In-place L2 normalisation of `nheads` heads of width `d` at column `offset`
    /// of `t` rows of `stride`.
    #[allow(clippy::too_many_arguments)]
    fn l2norm_heads(
        &self,
        x: &mut Self::F32,
        t: usize,
        stride: usize,
        offset: usize,
        nheads: usize,
        d: usize,
        eps: f32,
    ) -> Result<()>;
    /// Gated RMSNorm: rows `r = token * per_token + i` of `x` (`[rows, d]`), each
    /// multiplied by `sigmoid` of the matching `d` values of `z` at
    /// `z_off + token * z_stride + i * d`.
    #[allow(clippy::too_many_arguments)]
    fn gated_rmsnorm_sigmoid(
        &self,
        x: &Self::F32,
        z: &Self::F32,
        z_off: usize,
        z_stride: usize,
        per_token: usize,
        w: &Self::Bf16,
        out: &mut Self::F32,
        rows: usize,
        d: usize,
        eps: f32,
    ) -> Result<()>;
}

/// Hyper-connections: mixing `c` residual streams of width `h`.
pub trait HyperConnection: Memory {
    /// Splits a `[t, lr + c]` projection into `act = silu(down * inv_c)` `[t, lr]`
    /// and `inject = 2 * sigmoid(logit * inv_c)` `[t, c]`.
    #[allow(clippy::too_many_arguments)]
    fn hc_post_down(
        &self,
        fused: &Self::F32,
        lr: usize,
        c: usize,
        inv_c: f32,
        act: &mut Self::F32,
        inject: &mut Self::F32,
        t: usize,
    ) -> Result<()>;
    /// `mixed[t, h] = mean_c sigmoid(up[t, c, h]) * normed[t, c, h]`.
    fn hc_mix(
        &self,
        up: &Self::F32,
        normed: &Self::F32,
        mixed: &mut Self::F32,
        t: usize,
        c: usize,
        h: usize,
    ) -> Result<()>;
    /// `out[t, c, h] = res[t, c, h] + y[t, h] * inject[t, c]`.
    #[allow(clippy::too_many_arguments)]
    fn hc_combine(
        &self,
        res: &Self::F32,
        y: &Self::F32,
        inject: &Self::F32,
        out: &mut Self::F32,
        t: usize,
        c: usize,
        h: usize,
    ) -> Result<()>;
}

/// Recurrent token mixers and short convolutions with carried state.
pub trait Recurrent: Memory {
    /// Depthwise causal conv (`k` taps) and SiLU over `t` rows of `d` channels read
    /// at `x_off + row * x_stride`; `state` (`[d, k]`, oldest first) is updated.
    #[allow(clippy::too_many_arguments)]
    fn causal_conv_silu(
        &self,
        x: &Self::F32,
        x_off: usize,
        x_stride: usize,
        state: &mut Self::F32,
        w: &Self::Bf16,
        out: &mut Self::F32,
        t: usize,
        d: usize,
        k: usize,
    ) -> Result<()>;
    /// Gated DeltaNet gates: `g = -exp(a_log) * softplus(a + dt_bias)` and
    /// `beta = sigmoid(b)`, with `a` and `b` `[t, hv]` column ranges at `a_off` and
    /// `b_off` of rows of `ab_stride` in `ab`.
    #[allow(clippy::too_many_arguments)]
    fn gdn_gates(
        &self,
        ab: &Self::F32,
        a_off: usize,
        b_off: usize,
        ab_stride: usize,
        a_log: &Self::Bf16,
        dt_bias: &Self::Bf16,
        g: &mut Self::F32,
        beta: &mut Self::F32,
        t: usize,
        hv: usize,
    ) -> Result<()>;
    /// Exact gated delta rule over `t` tokens of `qkv` (rows of `stride`: `hk` query
    /// heads, `hk` key heads, then `hv` value heads), updating `state`
    /// `[hv, dk, dv]`.
    #[allow(clippy::too_many_arguments)]
    fn gdn_recurrent(
        &self,
        qkv: &Self::F32,
        g: &Self::F32,
        beta: &Self::F32,
        state: &mut Self::F32,
        out: &mut Self::F32,
        t: usize,
        stride: usize,
        hk: usize,
        hv: usize,
        dk: usize,
        dv: usize,
    ) -> Result<()>;
    /// `out = gated + silu(dilated_conv(x))` over `t` rows of `d` channels (`k`
    /// taps, dilation `dil`); `state` holds the previous `(k - 1) * dil` inputs.
    #[allow(clippy::too_many_arguments)]
    fn dilated_conv_silu_add(
        &self,
        x: &Self::F32,
        state: &mut Self::F32,
        w: &Self::Bf16,
        gated: &Self::F32,
        out: &mut Self::F32,
        t: usize,
        d: usize,
        k: usize,
        dil: usize,
    ) -> Result<()>;
}

/// Softmax attention with rotary positions and block-sparse key selection.
pub trait Attention: Memory + Sized {
    /// Rotate-half RoPE on the first `rd` dims of `[ntok, nheads, d]` rows; token `i`
    /// is at position `pos_base + i * pos_stride`; `inv_freq` has `rd / 2` entries.
    #[allow(clippy::too_many_arguments)]
    fn rope_rotate_half(
        &self,
        x: &mut Self::F32,
        ntok: usize,
        nheads: usize,
        d: usize,
        rd: usize,
        inv_freq: &Self::F32,
        pos_base: usize,
        pos_stride: usize,
    ) -> Result<()>;
    /// Splits `qg` `[t, heads, 2 * d]` into `q` `[t, heads, d]` and `gate`.
    fn split_q_gate(
        &self,
        qg: &Self::F32,
        q: &mut Self::F32,
        gate: &mut Self::F32,
        t: usize,
        heads: usize,
        d: usize,
    ) -> Result<()>;
    /// Block scores of `t` queries starting at position `start`: row `i` of `scores`
    /// (stride `kv_stride / ratio + 1`) holds `(start + i + 1) / ratio` scores,
    /// `sum_h relu(q_h . k_b) / sqrt(head_dim)`.
    #[allow(clippy::too_many_arguments)]
    fn qsa_scores(
        &self,
        q: &Self::F32,
        block_keys: &Self::F32,
        scores: &mut Self::F32,
        t: usize,
        start: usize,
        nheads: usize,
        head_dim: usize,
        ratio: usize,
        kv_stride: usize,
    ) -> Result<()>;
    /// Selection mask `[t, kv_stride]` (bytes): causal, restricted to each query's
    /// top `topk` blocks (ties to the lower block) plus its incomplete tail block.
    #[allow(clippy::too_many_arguments)]
    fn qsa_mask(
        &self,
        scores: &Self::F32,
        mask: &mut Self::Bytes,
        t: usize,
        start: usize,
        ratio: usize,
        topk: usize,
        kv_stride: usize,
    ) -> Result<()>;
    /// Appends `t` tokens of keys or values `src` (`[t, kv_heads, d]`) to a cache
    /// stored as `format` (its key or value half per `key`), at token `start`.
    #[allow(clippy::too_many_arguments)]
    fn kv_append(
        &self,
        src: &Self::F32,
        cache: &mut Self::Bytes,
        format: KvFormat,
        key: bool,
        start: usize,
        t: usize,
        kv_heads: usize,
        d: usize,
    ) -> Result<()>;
    /// The first `len` tokens of a cache as fp32 `[len, kv_heads, d]`: exact for
    /// [`KvFormat::F32`], decoded otherwise.
    #[allow(clippy::too_many_arguments)]
    fn kv_read(
        &self,
        cache: &Self::Bytes,
        dst: &mut Self::F32,
        format: KvFormat,
        key: bool,
        len: usize,
        kv_heads: usize,
        d: usize,
    ) -> Result<()>;
    /// Grouped-query attention of `t` queries over `kv_len` cached keys and values
    /// (stored as `format`, written by [`Attention::kv_append`]) under `mask`, fp32
    /// softmax; scratch comes from `ws`. No row of `mask` sets more than
    /// `max_visible` keys (`kv_len` when unknown); sparse selections let a backend
    /// list each query's keys in that much space.
    #[allow(clippy::too_many_arguments)]
    fn attention(
        &self,
        ws: &mut crate::Workspace<Self>,
        q: &Self::F32,
        k: &Self::Bytes,
        v: &Self::Bytes,
        format: KvFormat,
        mask: &Self::Bytes,
        out: &mut Self::F32,
        t: usize,
        heads: usize,
        kv_heads: usize,
        d: usize,
        kv_len: usize,
        max_visible: usize,
        scale: f32,
    ) -> Result<()>;
}

/// How an attention cache stores keys and values.
///
/// [`KvFormat::Turbo`] follows TurboQuant: every head vector is rotated by a fixed
/// randomized Walsh-Hadamard transform, which spreads its energy evenly over the
/// coordinates and preserves dot products, then each block of 32 coordinates keeps
/// an fp16 scale (its RMS) and a Lloyd-Max codebook index per coordinate for a unit
/// Gaussian. Attention rotates queries the same way and un-rotates its output, so
/// no step outside the backend sees rotated vectors. Lossy: judge it by outcome
/// (`oominf score`), not against the per-layer reference budgets.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KvFormat {
    /// Keys and values as computed, fp32.
    F32,
    /// Rotated, `k_bits` / `v_bits` (2 to 8) per coordinate plus a 16-bit scale per
    /// 32 coordinates.
    Turbo { k_bits: u8, v_bits: u8 },
}

impl KvFormat {
    /// Bits per coordinate of keys (`key`) or values; 0 for fp32.
    pub fn bits(self, key: bool) -> u8 {
        match self {
            KvFormat::F32 => 0,
            KvFormat::Turbo { k_bits, v_bits } => {
                if key {
                    k_bits
                } else {
                    v_bits
                }
            }
        }
    }

    /// Bytes of one cached head vector of `d` coordinates: fp32 values, or the
    /// block scales (padded to 16 bytes) and `bits` 32-bit bit-plane words per
    /// block of 32.
    pub fn row_bytes(self, key: bool, d: usize) -> usize {
        match self.bits(key) as usize {
            0 => 4 * d,
            b => (d / 32 * 2).next_multiple_of(16) + d / 32 * b * 4,
        }
    }

    /// Parses `fp32`, `tq<b>` (keys and values at `b` bits) or `k<b>v<b>`.
    pub fn parse(s: &str) -> anyhow::Result<Self> {
        let bits = |t: &str| -> anyhow::Result<u8> {
            match t.parse::<u8>() {
                Ok(b @ 2..=8) => Ok(b),
                _ => anyhow::bail!("KV cache bits must be 2 to 8, got {t:?}"),
            }
        };
        if s == "fp32" {
            return Ok(KvFormat::F32);
        }
        if let Some(b) = s.strip_prefix("tq") {
            let b = bits(b)?;
            return Ok(KvFormat::Turbo {
                k_bits: b,
                v_bits: b,
            });
        }
        if let Some((k, v)) = s.strip_prefix('k').and_then(|r| r.split_once('v')) {
            return Ok(KvFormat::Turbo {
                k_bits: bits(k)?,
                v_bits: bits(v)?,
            });
        }
        anyhow::bail!("unknown KV cache format {s:?} (fp32, tq4, k6v4, ...)")
    }

    /// Lloyd-Max codebooks for a unit Gaussian at 2 to 8 bits, ascending, concatenated:
    /// the `2^b` levels for `b` bits start at [`KvFormat::codebook_offset`]. Computed
    /// once (deterministic f64 iteration); backends upload the table.
    pub fn codebooks() -> &'static [f32] {
        static TABLE: std::sync::OnceLock<Vec<f32>> = std::sync::OnceLock::new();
        TABLE.get_or_init(|| (2..=8).flat_map(lloyd_max_gaussian).collect())
    }

    /// Index of the first level for `bits` in [`KvFormat::codebooks`].
    pub fn codebook_offset(bits: u8) -> usize {
        (1usize << bits) - 4
    }
}

/// The `2^bits` Lloyd-Max levels for a unit Gaussian: alternately place thresholds
/// halfway between levels and move each level to the mean of its cell, until no
/// level moves by more than 1e-12.
fn lloyd_max_gaussian(bits: u8) -> Vec<f32> {
    let n = 1usize << bits;
    let pdf = |x: f64| (-0.5 * x * x).exp() / (2.0 * std::f64::consts::PI).sqrt();
    // Upper-tail probability Q(x) = P(X > x), from erfc (Numerical Recipes erfcc,
    // fractional error below 1.2e-7), symmetric so tails keep their precision.
    let q = |x: f64| {
        let z = x.abs() / std::f64::consts::SQRT_2;
        let t = 1.0 / (1.0 + 0.5 * z);
        let erfc = t
            * (-z * z - 1.26551223
                + t * (1.00002368
                    + t * (0.37409196
                        + t * (0.09678418
                            + t * (-0.18628806
                                + t * (0.27886807
                                    + t * (-1.13520398
                                        + t * (1.48851587
                                            + t * (-0.82215223 + t * 0.17087277)))))))))
                .exp();
        if x >= 0.0 {
            0.5 * erfc
        } else {
            1.0 - 0.5 * erfc
        }
    };
    let span = 4.0 + bits as f64 * 0.25;
    let mut c: Vec<f64> = (0..n)
        .map(|i| -span + 2.0 * span * (i as f64 + 0.5) / n as f64)
        .collect();
    for _ in 0..200_000 {
        let edge = |i: usize| -> f64 {
            match i {
                0 => f64::NEG_INFINITY,
                i if i == n => f64::INFINITY,
                i => 0.5 * (c[i - 1] + c[i]),
            }
        };
        let mut moved = 0f64;
        let next: Vec<f64> = (0..n)
            .map(|i| {
                let (a, b) = (edge(i), edge(i + 1));
                let (pa, pb) = (
                    if a.is_finite() { pdf(a) } else { 0.0 },
                    if b.is_finite() { pdf(b) } else { 0.0 },
                );
                let mass = q(a) - q(b);
                let m = if mass > 0.0 { (pa - pb) / mass } else { c[i] };
                moved = moved.max((m - c[i]).abs());
                m
            })
            .collect();
        c = next;
        if moved < 1e-12 {
            break;
        }
    }
    c.into_iter().map(|v| v as f32).collect()
}

#[cfg(test)]
mod tests {
    use super::KvFormat;

    #[test]
    fn codebooks_match_known_lloyd_max_levels() {
        let t = KvFormat::codebooks();
        assert_eq!(t.len(), (2..=8).map(|b| 1usize << b).sum::<usize>());
        let level = |bits: u8, i: usize| t[KvFormat::codebook_offset(bits) + i];
        // Max (1960): 3 bits 0.2451, 0.7560, 1.3440, 2.1520; 4 bits 0.1284 ... 2.7326.
        for (bits, known) in [
            (3u8, &[0.2451f32, 0.7560, 1.3440, 2.1520][..]),
            (
                4,
                &[
                    0.1284, 0.3881, 0.6568, 0.9424, 1.2562, 1.6181, 2.0690, 2.7326,
                ][..],
            ),
        ] {
            let half = 1usize << (bits - 1);
            for (i, &k) in known.iter().enumerate() {
                assert!(
                    (level(bits, half + i) - k).abs() < 2e-3,
                    "{bits}-bit level {i}"
                );
                assert!(
                    (level(bits, half - 1 - i) + k).abs() < 2e-3,
                    "{bits}-bit level -{i}"
                );
            }
        }
        for bits in 2..=8u8 {
            let n = 1usize << bits;
            let l = &t[KvFormat::codebook_offset(bits)..][..n];
            assert!(
                l.windows(2).all(|w| w[0] < w[1]),
                "{bits}-bit levels ascend"
            );
        }
    }

    #[test]
    fn parses_formats() {
        assert_eq!(KvFormat::parse("fp32").unwrap(), KvFormat::F32);
        assert_eq!(
            KvFormat::parse("k6v4").unwrap(),
            KvFormat::Turbo {
                k_bits: 6,
                v_bits: 4
            }
        );
        assert!(KvFormat::parse("tq9").is_err());
        assert_eq!(
            KvFormat::Turbo {
                k_bits: 4,
                v_bits: 4
            }
            .row_bytes(true, 256),
            16 + 8 * 16
        );
    }
}

/// Byte layout of an NVFP4 expert record: part offsets and the index of each
/// projection's `weight_scale_2` among the record's leading f32 scalars.
#[derive(Debug, Clone, Copy)]
pub struct Nvfp4Record {
    /// Model width (gate/up input, down output).
    pub hidden: usize,
    /// Expert intermediate width (gate/up output, down input).
    pub inter: usize,
    pub gate_weight: usize,
    pub gate_scale: usize,
    pub up_weight: usize,
    pub up_scale: usize,
    pub down_weight: usize,
    pub down_scale: usize,
    /// `weight_scale_2` indices for gate, up, down.
    pub scale2: [usize; 3],
}

/// Byte offsets of the parts of a bf16 expert record (weights as released,
/// row-major): gate and up `[inter, hidden]`, down `[hidden, inter]`.
#[derive(Debug, Clone, Copy)]
pub struct Bf16Record {
    pub hidden: usize,
    pub inter: usize,
    pub gate_weight: usize,
    pub up_weight: usize,
    pub down_weight: usize,
}

/// Routing and routed-expert compute on quantised records read in place.
///
/// Expert groups are described by `recs` (record addresses, one per expert),
/// `off` (assignment ranges, `off[e]..off[e + 1]`) and per-assignment tables.
pub trait Experts: Memory {
    /// Softmax over `e` logits per row, top `k` (ties to the lower id), weights
    /// renormalised over the top `k`.
    fn router_topk(
        &self,
        logits: &Self::F32,
        ids: &mut Self::I32,
        weights: &mut Self::F32,
        t: usize,
        e: usize,
        k: usize,
    ) -> Result<()>;
    /// `h[a] = silu(x[tok(a)] . Wg) * (x[tok(a)] . Wu)` for every assignment.
    #[allow(clippy::too_many_arguments)]
    fn moe_gate_up(
        &self,
        recs: &View<Self::U64>,
        off: &View<Self::I32>,
        assign_tok: &View<Self::I32>,
        n_experts: usize,
        x: &Self::F32,
        h: &mut Self::F32,
        geo: &Nvfp4Record,
    ) -> Result<()>;
    /// [`Experts::moe_gate_up`] over bf16 records (decode-sized steps).
    #[allow(clippy::too_many_arguments)]
    fn moe_gate_up_bf16(
        &self,
        recs: &View<Self::U64>,
        off: &View<Self::I32>,
        assign_tok: &View<Self::I32>,
        n_experts: usize,
        x: &Self::F32,
        h: &mut Self::F32,
        geo: &Bf16Record,
    ) -> Result<()>;
    /// [`Experts::moe_down`] over bf16 records (decode-sized steps).
    fn moe_down_bf16(
        &self,
        recs: &View<Self::U64>,
        off: &View<Self::I32>,
        n_experts: usize,
        h: &Self::F32,
        y: &mut Self::F32,
        geo: &Bf16Record,
    ) -> Result<()>;
    /// `y[a] = h[a] . Wd^T` for every assignment.
    fn moe_down(
        &self,
        recs: &View<Self::U64>,
        off: &View<Self::I32>,
        n_experts: usize,
        h: &Self::F32,
        y: &mut Self::F32,
        geo: &Nvfp4Record,
    ) -> Result<()>;
    /// Tiled grouped GEMM for large steps: `y[a] = x[row(a)] . W^T` for one
    /// projection `(weight offset, scale offset, scale2 index)`, with
    /// `row(a) = rows[a]`, or `a` without `rows`.
    #[allow(clippy::too_many_arguments)]
    fn moe_tiled(
        &self,
        recs: &View<Self::U64>,
        off: &View<Self::I32>,
        rows: Option<&View<Self::I32>>,
        n_experts: usize,
        max_per_expert: usize,
        x: &Self::F32,
        y: &mut Self::F32,
        n: usize,
        k: usize,
        proj: (usize, usize, usize),
    ) -> Result<()>;
    /// `h[i] = silu(g[i]) * u[i]` for `i` in `[start, end)`.
    fn moe_swiglu_range(
        &self,
        g: &Self::F32,
        u: &Self::F32,
        h: &mut Self::F32,
        start: usize,
        end: usize,
    ) -> Result<()>;
    /// `out[t] = sum_s w[t, s] * y[slot_assign[t, s]]`, summed in slot order.
    #[allow(clippy::too_many_arguments)]
    fn moe_combine_slots(
        &self,
        y: &Self::F32,
        slot_assign: &View<Self::I32>,
        w: &Self::F32,
        out: &mut Self::F32,
        t: usize,
        h: usize,
        k: usize,
    ) -> Result<()>;
    /// `out = routed + sigmoid(gate_logit[t]) * shared`.
    fn moe_combine(
        &self,
        routed: &Self::F32,
        shared: &Self::F32,
        gate_logit: &Self::F32,
        out: &mut Self::F32,
        t: usize,
        h: usize,
    ) -> Result<()>;
}

/// Gathered quantised embedding rows and per-stream gating.
pub trait Lookup: Memory {
    /// `out = fp8_e4m3(rows) * scale`.
    fn fp8_dequant_scaled(
        &self,
        rows: &Self::Bytes,
        scale: f32,
        out: &mut Self::F32,
        n: usize,
    ) -> Result<()>;
    /// Per stream `c`: `gated[t, c] = sigmoid(signed_sqrt(key[t, c] . query[t, c] /
    /// sqrt(h))) * value[t]`.
    #[allow(clippy::too_many_arguments)]
    fn ple_gate(
        &self,
        key: &Self::F32,
        query: &Self::F32,
        value: &Self::F32,
        gated: &mut Self::F32,
        t: usize,
        c: usize,
        h: usize,
    ) -> Result<()>;
}

/// Copies that run alongside compute, from pinned host memory or within the device,
/// ordered against compute with events.
pub trait Transfer: Memory {
    /// An ordered queue of copies that runs concurrently with compute.
    type CopyQueue;
    type Event;
    /// A download started by [`Transfer::download_start_i32`] or `_f32`.
    type Download;

    fn copy_queue(&self) -> Result<Self::CopyQueue>;
    /// Makes `len` bytes of host memory at `ptr` usable as a source of asynchronous
    /// copies (a no-op where the platform does not need it).
    ///
    /// # Safety
    /// `ptr` must stay valid until [`Transfer::unpin_host`].
    unsafe fn pin_host(&self, ptr: *mut u8, len: usize) -> Result<()>;
    /// # Safety
    /// `ptr` must have been pinned with [`Transfer::pin_host`].
    unsafe fn unpin_host(&self, ptr: *mut u8);
    /// Enqueues a copy of `len` bytes from pinned host memory to device address
    /// `dst`.
    ///
    /// # Safety
    /// The source must stay unchanged and the destination unread by other work
    /// until the copy completes.
    unsafe fn copy_to_device(
        &self,
        queue: &Self::CopyQueue,
        dst: u64,
        src: *const u8,
        len: usize,
    ) -> Result<()>;
    /// Enqueues, after the compute issued so far, a copy of the first `n` elements
    /// of `src` into pinned host memory at `dst`; [`Transfer::record_compute`] then
    /// [`Transfer::event_wait`] tell when it has landed.
    ///
    /// # Safety
    /// `dst` must be pinned with [`Transfer::pin_host`], hold `n` elements and stay
    /// unread and valid until the copy completes.
    unsafe fn download_async(&self, src: &Self::I32, dst: *mut i32, n: usize) -> Result<()>;
    /// Queues, after the compute issued so far, a copy of the first `n` elements of
    /// `src` to the host; [`Transfer::download_wait_i32`] waits and returns them, so
    /// the host can queue more work first.
    fn download_start_i32(&self, src: &Self::I32, n: usize) -> Result<Self::Download>;
    /// As [`Transfer::download_start_i32`] for `f32` (see
    /// [`Transfer::download_wait_f32`]).
    fn download_start_f32(&self, src: &Self::F32, n: usize) -> Result<Self::Download>;
    fn download_wait_i32(&self, pending: Self::Download) -> Result<Vec<i32>>;
    fn download_wait_f32(&self, pending: Self::Download) -> Result<Vec<f32>>;
    /// Enqueues a device-to-device copy of `len` bytes.
    ///
    /// # Safety
    /// As for [`Transfer::copy_to_device`].
    unsafe fn copy_on_device(
        &self,
        queue: &Self::CopyQueue,
        dst: u64,
        src: u64,
        len: usize,
    ) -> Result<()>;
    /// An event that completes when all compute issued so far has.
    fn record_compute(&self) -> Result<Self::Event>;
    /// An event that completes when all copies queued on `queue` so far have.
    fn record_copies(&self, queue: &Self::CopyQueue) -> Result<Self::Event>;
    /// Copies queued later on `queue` wait for `event`.
    fn copies_wait(&self, queue: &Self::CopyQueue, event: &Self::Event) -> Result<()>;
    /// Compute issued later waits for `event`.
    fn compute_wait(&self, event: &Self::Event) -> Result<()>;
    fn event_done(&self, event: &Self::Event) -> Result<bool>;
    /// Blocks the host until `event` completes.
    fn event_wait(&self, event: &Self::Event) -> Result<()>;
    /// Blocks the host until every copy queued on `queue` completes.
    fn copies_sync(&self, queue: &Self::CopyQueue) -> Result<()>;
}

/// Everything a model needs from a platform.
pub trait Backend:
    Linear
    + Elementwise
    + Norm
    + HyperConnection
    + Recurrent
    + Attention
    + Experts
    + Lookup
    + Transfer
    + Sized
    + 'static
{
}

impl<T> Backend for T where
    T: Linear
        + Elementwise
        + Norm
        + HyperConnection
        + Recurrent
        + Attention
        + Experts
        + Lookup
        + Transfer
        + Sized
        + 'static
{
}
