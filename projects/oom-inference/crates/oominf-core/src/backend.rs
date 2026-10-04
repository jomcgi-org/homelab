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

    fn upload_bf16(&self, host: &[u16]) -> Result<Self::Bf16>;
    /// bf16 buffer of `n` elements with unspecified contents.
    fn uninit_bf16(&self, n: usize) -> Result<Self::Bf16>;

    fn upload_bytes(&self, host: &[u8]) -> Result<Self::Bytes>;
    fn uninit_bytes(&self, n: usize) -> Result<Self::Bytes>;
    fn zeros_bytes(&self, n: usize) -> Result<Self::Bytes>;
    fn download_bytes(&self, buf: &Self::Bytes) -> Result<Vec<u8>>;

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
pub trait Linear: Memory {
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
    /// Grouped-query attention of `t` queries over `kv_len` cached keys under
    /// `mask`, fp32 softmax; scratch comes from `ws`.
    #[allow(clippy::too_many_arguments)]
    fn attention(
        &self,
        ws: &mut crate::Workspace<Self>,
        q: &Self::F32,
        k: &Self::F32,
        v: &Self::F32,
        mask: &Self::Bytes,
        out: &mut Self::F32,
        t: usize,
        heads: usize,
        kv_heads: usize,
        d: usize,
        kv_len: usize,
        scale: f32,
    ) -> Result<()>;
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
