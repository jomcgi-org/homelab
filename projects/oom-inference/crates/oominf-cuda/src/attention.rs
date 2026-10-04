//! Rotary attention with block-sparse (QSA) key selection: `kernels/attention.cu`.

use anyhow::Result;
use cudarc::driver::sys::CUfunction_attribute;
use cudarc::driver::{CudaView, CudaViewMut, LaunchConfig, PushKernelArg};
use oominf_core::{Attention, Workspace};

use crate::{Buf, Dev, Gpu, grid};

impl Attention for Gpu {
    /// Rotate-half RoPE on the first `rd` dims of `[ntok, nheads, d]` rows; token `i`
    /// sits at position `pos_base + i * pos_stride`. `inv_freq` has `rd / 2` entries.
    #[allow(clippy::too_many_arguments)]
    fn rope_rotate_half(
        &self,
        x: &mut Buf,
        ntok: usize,
        nheads: usize,
        d: usize,
        rd: usize,
        inv_freq: &Buf,
        pos_base: usize,
        pos_stride: usize,
    ) -> Result<()> {
        self.check(
            rd <= d && rd.is_multiple_of(2) && inv_freq.len() >= rd / 2,
            "rope dims",
        )?;
        let f = self.func("rope_rotate_half")?;
        let (n32, h32, d32, r32) = (ntok as i32, nheads as i32, d as i32, rd as i32);
        let (b32, s32) = (pos_base as i32, pos_stride as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(x)
                .arg(&n32)
                .arg(&h32)
                .arg(&d32)
                .arg(&r32)
                .arg(inv_freq)
                .arg(&b32)
                .arg(&s32)
                .launch(grid(ntok * nheads * rd / 2, 256))?
        };
        Ok(())
    }

    /// Splits `qg` `[t, heads, 2 * d]` into `q` `[t, heads, d]` and `gate` `[t, heads * d]`.
    fn split_q_gate(
        &self,
        qg: &Buf,
        q: &mut Buf,
        gate: &mut Buf,
        t: usize,
        heads: usize,
        d: usize,
    ) -> Result<()> {
        let f = self.func("split_q_gate")?;
        let (t32, h32, d32) = (t as i32, heads as i32, d as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(qg)
                .arg(q)
                .arg(gate)
                .arg(&t32)
                .arg(&h32)
                .arg(&d32)
                .launch(grid(t * heads * d, 256))?
        };
        Ok(())
    }

    /// QSA block scores for `t` queries starting at position `start`: row `i` of
    /// `scores` (stride `kv_stride / ratio + 1`) holds `(start + i + 1) / ratio` scores.
    #[allow(clippy::too_many_arguments)]
    fn qsa_scores(
        &self,
        q: &Buf,
        block_keys: &Buf,
        scores: &mut Buf,
        t: usize,
        start: usize,
        nheads: usize,
        head_dim: usize,
        ratio: usize,
        kv_stride: usize,
    ) -> Result<()> {
        self.check(
            scores.len() >= t * (kv_stride / ratio + 1)
                && start + t <= kv_stride
                && nheads * head_dim <= 512
                && head_dim <= 128,
            "qsa_scores sizes",
        )?;
        let f = self.func("qsa_scores")?;
        f.set_attribute(
            CUfunction_attribute::CU_FUNC_ATTRIBUTE_MAX_DYNAMIC_SHARED_SIZE_BYTES,
            QSA_SMEM_BYTES as i32,
        )?;
        let a = [t, start, nheads, head_dim, ratio, kv_stride].map(|v| v as i32);
        let nb_max = (start + t) / ratio;
        let cfg = LaunchConfig {
            grid_dim: (nb_max.div_ceil(64).max(1) as u32, t.div_ceil(16) as u32, 1),
            block_dim: (256, 1, 1),
            shared_mem_bytes: QSA_SMEM_BYTES as u32,
        };
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(q)
                .arg(block_keys)
                .arg(scores)
                .arg(&a[0])
                .arg(&a[1])
                .arg(&a[2])
                .arg(&a[3])
                .arg(&a[4])
                .arg(&a[5])
                .launch(cfg)?
        };
        Ok(())
    }

    /// QSA selection from `scores`: writes `mask` `[t, kv_stride]` (1 = attendable),
    /// keeping each query's top `topk` blocks (equal scores to the lower block) and the
    /// incomplete tail block.
    #[allow(clippy::too_many_arguments)]
    fn qsa_mask(
        &self,
        scores: &Buf,
        mask: &mut Dev<u8>,
        t: usize,
        start: usize,
        ratio: usize,
        topk: usize,
        kv_stride: usize,
    ) -> Result<()> {
        let flags = kv_stride / ratio + 1;
        self.check(
            scores.len() >= t * flags && mask.len() >= t * kv_stride && start + t <= kv_stride,
            "qsa_mask sizes",
        )?;
        self.check(
            flags <= 48 * 1024,
            "qsa_mask: context too long for shared flags",
        )?;
        let f = self.func("qsa_mask")?;
        let a = [t, start, ratio, topk, kv_stride].map(|v| v as i32);
        let cfg = LaunchConfig {
            grid_dim: (t as u32, 1, 1),
            block_dim: (256, 1, 1),
            shared_mem_bytes: flags as u32,
        };
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(scores)
                .arg(mask)
                .arg(&a[0])
                .arg(&a[1])
                .arg(&a[2])
                .arg(&a[3])
                .arg(&a[4])
                .launch(cfg)?
        };
        Ok(())
    }

    /// Attention of `t` queries over `kv_len` cached keys under `mask` (`[t, kv_len]`
    /// bytes). Up to [`DECODE_MAX_TOKENS`] query tokens with `d == 256` (decode and
    /// draft verification) take the GQA flash-decode path one token at a time (split
    /// over keys, merged online-softmax partials), which keeps the GPU busy for a
    /// handful of queries; longer steps use `attn_prefill`.
    #[allow(clippy::too_many_arguments)]
    fn attention(
        &self,
        ws: &mut Workspace<Gpu>,
        q: &Buf,
        k: &Buf,
        v: &Buf,
        mask: &Dev<u8>,
        out: &mut Buf,
        t: usize,
        heads: usize,
        kv_heads: usize,
        d: usize,
        kv_len: usize,
        scale: f32,
    ) -> Result<()> {
        let g = heads / kv_heads;
        // The decode kernel is instantiated for the GQA group size the models use.
        if t > DECODE_MAX_TOKENS || d != 256 || g != 12 || !heads.is_multiple_of(kv_heads) {
            return self.attn_prefill(
                q, k, v, mask, out, t, heads, kv_heads, d, kv_len, kv_len, scale,
            );
        }
        let row = heads * d;
        for i in 0..t {
            self.flash_decode(
                ws,
                &q.0.slice(i * row..(i + 1) * row),
                k,
                v,
                &mask.0.slice(i * kv_len..(i + 1) * kv_len),
                &mut out.0.slice_mut(i * row..(i + 1) * row),
                heads,
                kv_heads,
                d,
                kv_len,
                scale,
            )?;
        }
        Ok(())
    }
}

/// Most query tokens that take the flash-decode path, one token at a time.
const DECODE_MAX_TOKENS: usize = 4;

impl Gpu {
    /// Flash-decode of one query token (`q`, `mask` row and `out` of that token).
    #[allow(clippy::too_many_arguments)]
    fn flash_decode(
        &self,
        ws: &mut Workspace<Gpu>,
        q: &CudaView<f32>,
        k: &Buf,
        v: &Buf,
        mask: &CudaView<u8>,
        out: &mut CudaViewMut<f32>,
        heads: usize,
        kv_heads: usize,
        d: usize,
        kv_len: usize,
        scale: f32,
    ) -> Result<()> {
        let g = heads / kv_heads;
        // Small chunks keep enough warps busy at short contexts; long contexts get
        // larger chunks so the partial count (and the combine) stays bounded.
        const WARPS: usize = 8;
        let chunk = kv_len.div_ceil(1024).clamp(16, 1024);
        let blocks = kv_len.div_ceil(chunk * WARPS);
        let p = blocks * WARPS;
        let mut part = ws.take(self, "attn.fd_part", kv_heads * p * g * (d + 2))?;
        let f = self.func("attn_decode_partial_g12")?;
        let (h32, kvh32, kv32, c32) = (heads as i32, kv_heads as i32, kv_len as i32, chunk as i32);
        let cfg = LaunchConfig {
            grid_dim: (blocks as u32, kv_heads as u32, 1),
            block_dim: ((WARPS * 32) as u32, 1, 1),
            shared_mem_bytes: 0,
        };
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(q)
                .arg(k)
                .arg(v)
                .arg(mask)
                .arg(&mut part)
                .arg(&kvh32)
                .arg(&kv32)
                .arg(&c32)
                .arg(&scale)
                .launch(cfg)?
        };
        let f = self.func("attn_decode_combine")?;
        let p32 = p as i32;
        let cfg = LaunchConfig {
            grid_dim: (heads as u32, 1, 1),
            block_dim: (d as u32, 1, 1),
            shared_mem_bytes: 0,
        };
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(&part)
                .arg(out)
                .arg(&h32)
                .arg(&kvh32)
                .arg(&p32)
                .launch(cfg)?
        };
        ws.give("attn.fd_part", part);
        Ok(())
    }
}

/// `qsa_scores`' shared memory: 16 queries of up to 512 floats and 64 key blocks of
/// up to 128 floats (rows padded to 129).
const QSA_SMEM_BYTES: usize = 4 * (16 * 512 + 64 * 129);

/// Query rows (tokens x heads sharing a KV head) per `attn_prefill` block.
const ATTN_ROWS: usize = 48;
/// `attn_prefill`'s shared memory: Q rows and a key tile (padded rows of 260
/// floats), a value tile, the tile's scores and three per-row statistics.
const ATTN_SMEM_BYTES: usize =
    4 * (ATTN_ROWS * 260 + 16 * 260 + 16 * 256 + ATTN_ROWS * 16 + 3 * ATTN_ROWS);

impl Gpu {
    /// Masked GQA attention of `t` queries over the first `kv_len` cache rows with an
    /// online softmax (no `[t, heads, kv]` score matrix). Needs `d <= 256`.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn attn_prefill(
        &self,
        q: &Buf,
        k: &Buf,
        v: &Buf,
        mask: &Dev<u8>,
        out: &mut Buf,
        t: usize,
        heads: usize,
        kv_heads: usize,
        d: usize,
        kv_len: usize,
        kv_stride: usize,
        scale: f32,
    ) -> Result<()> {
        let g = heads / kv_heads.max(1);
        self.check(
            d <= 256
                && d.is_multiple_of(4)
                && kv_len <= kv_stride
                && mask.len() >= t * kv_stride
                && heads.is_multiple_of(kv_heads)
                && g <= ATTN_ROWS,
            "attn_prefill sizes",
        )?;
        let f = self.func("attn_prefill")?;
        f.set_attribute(
            CUfunction_attribute::CU_FUNC_ATTRIBUTE_MAX_DYNAMIC_SHARED_SIZE_BYTES,
            ATTN_SMEM_BYTES as i32,
        )?;
        // Tokens per block: as many as fill the block's query rows.
        let tq = ATTN_ROWS / g;
        let a = [t, heads, kv_heads, d, kv_len, kv_stride].map(|v| v as i32);
        let tq32 = tq as i32;
        let cfg = LaunchConfig {
            grid_dim: (t.div_ceil(tq) as u32, kv_heads as u32, 1),
            block_dim: (256, 1, 1),
            shared_mem_bytes: ATTN_SMEM_BYTES as u32,
        };
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(q)
                .arg(k)
                .arg(v)
                .arg(mask)
                .arg(out)
                .arg(&a[0])
                .arg(&a[1])
                .arg(&a[2])
                .arg(&a[3])
                .arg(&a[4])
                .arg(&a[5])
                .arg(&scale)
                .arg(&tq32)
                .launch(cfg)?
        };
        Ok(())
    }
}
