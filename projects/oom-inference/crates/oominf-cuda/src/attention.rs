//! Rotary attention with block-sparse (QSA) key selection: `kernels/attention.cu`.

use anyhow::Result;
use cudarc::driver::sys::CUfunction_attribute;
use cudarc::driver::{CudaView, CudaViewMut, DevicePtr, LaunchConfig, PushKernelArg};
use oominf_core::{Attention, Elementwise, KvFormat, Memory, Workspace};

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
        // Keep flags as bits; the kernel's static shared memory takes about 1.2 KB.
        let flag_bytes = flags.div_ceil(32) * 4;
        self.check(
            scores.len() >= t * flags && mask.len() >= t * kv_stride && start + t <= kv_stride,
            "qsa_mask sizes",
        )?;
        self.check(
            flag_bytes <= 46 * 1024,
            "qsa_mask: context too long for shared flags",
        )?;
        let f = self.func("qsa_mask")?;
        let a = [t, start, ratio, topk, kv_stride].map(|v| v as i32);
        let cfg = LaunchConfig {
            grid_dim: (t as u32, 1, 1),
            block_dim: (1024, 1, 1),
            shared_mem_bytes: flag_bytes as u32,
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

    fn kv_append(
        &self,
        src: &Buf,
        cache: &mut Dev<u8>,
        format: KvFormat,
        key: bool,
        start: usize,
        t: usize,
        kv_heads: usize,
        d: usize,
    ) -> Result<()> {
        let (bits, row) = (format.bits(key), format.row_bytes(key, d));
        let rows = t * kv_heads;
        self.check(
            kv_row_shape(d, bits)
                && src.len() >= rows * d
                && cache.len() >= (start * kv_heads + rows) * row,
            "kv_append sizes",
        )?;
        if rows == 0 {
            return Ok(());
        }
        let f = self.func("kv_append")?;
        let levels = self.kv_codebooks(format)?;
        let (row0, d32, b32) = ((start * kv_heads) as i64, d as i32, bits as i32);
        let cfg = LaunchConfig {
            grid_dim: (rows as u32, 1, 1),
            block_dim: (d as u32, 1, 1),
            shared_mem_bytes: 0,
        };
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(src)
                .arg(cache)
                .arg(&row0)
                .arg(&d32)
                .arg(&b32)
                .arg(&levels)
                .launch(cfg)?
        };
        Ok(())
    }

    fn kv_read(
        &self,
        cache: &Dev<u8>,
        dst: &mut Buf,
        format: KvFormat,
        key: bool,
        len: usize,
        kv_heads: usize,
        d: usize,
    ) -> Result<()> {
        let (bits, row) = (format.bits(key), format.row_bytes(key, d));
        let rows = len * kv_heads;
        self.check(
            kv_row_shape(d, bits) && dst.len() >= rows * d && cache.len() >= rows * row,
            "kv_read sizes",
        )?;
        if rows == 0 {
            return Ok(());
        }
        let f = self.func("kv_read")?;
        let levels = self.kv_codebooks(format)?;
        let (d32, b32) = (d as i32, bits as i32);
        let cfg = LaunchConfig {
            grid_dim: (rows as u32, 1, 1),
            block_dim: (d as u32, 1, 1),
            shared_mem_bytes: 0,
        };
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(cache)
                .arg(dst)
                .arg(&d32)
                .arg(&b32)
                .arg(&levels)
                .launch(cfg)?
        };
        Ok(())
    }

    /// Attention of `t` queries over `kv_len` cached keys under `mask` (`[t, kv_len]`
    /// bytes). Up to [`DECODE_MAX_TOKENS`] query tokens with `d == 256` (decode and
    /// draft verification) take the GQA flash-decode path one token at a time (split
    /// over keys, merged online-softmax partials), which keeps the GPU busy for a
    /// handful of queries; longer steps use `attn_prefill`. A rotated (Turbo) cache
    /// gets rotated queries, and the output is rotated back.
    #[allow(clippy::too_many_arguments)]
    fn attention(
        &self,
        ws: &mut Workspace<Gpu>,
        q: &Buf,
        k: &Dev<u8>,
        v: &Dev<u8>,
        format: KvFormat,
        mask: &Dev<u8>,
        out: &mut Buf,
        t: usize,
        heads: usize,
        kv_heads: usize,
        d: usize,
        kv_len: usize,
        scale: f32,
    ) -> Result<()> {
        let bits = (
            format.bits(true),
            format.bits(false),
            self.kv_codebooks(format)?,
        );
        let rotated = format != KvFormat::F32;
        let q_rot = if rotated {
            let mut r = ws.take(self, "attn.q_rot", t * heads * d)?;
            self.copy_range(q, 0, &mut r, 0, t * heads * d)?;
            self.kv_rotate(&mut r, t * heads, d, false)?;
            Some(r)
        } else {
            None
        };
        let qq = q_rot.as_ref().unwrap_or(q);
        let g = heads / kv_heads;
        // The decode kernel is instantiated for the GQA group size the models use.
        if t > DECODE_MAX_TOKENS || d != 256 || g != 12 || !heads.is_multiple_of(kv_heads) {
            self.attn_prefill(
                ws, qq, k, v, bits, mask, out, t, heads, kv_heads, d, kv_len, kv_len, scale,
            )?;
        } else {
            let row = heads * d;
            for i in 0..t {
                self.flash_decode(
                    ws,
                    &qq.0.slice(i * row..(i + 1) * row),
                    k,
                    v,
                    bits,
                    &mask.0.slice(i * kv_len..(i + 1) * kv_len),
                    &mut out.0.slice_mut(i * row..(i + 1) * row),
                    heads,
                    kv_heads,
                    d,
                    kv_len,
                    scale,
                )?;
            }
        }
        if let Some(r) = q_rot {
            ws.give("attn.q_rot", r);
            self.kv_rotate(out, t * heads, d, true)?;
        }
        Ok(())
    }
}

/// Whether rows of `d` coordinates can be stored at `bits` (0: fp32): one block of
/// `d` threads per row, and a power-of-two `d` for the rotation.
fn kv_row_shape(d: usize, bits: u8) -> bool {
    d.is_multiple_of(32)
        && d <= 256
        && (bits == 0 || (d.is_power_of_two() && (2..=8).contains(&bits)))
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
        k: &Dev<u8>,
        v: &Dev<u8>,
        (kb, vb, levels): (u8, u8, u64),
        mask: &CudaView<u8>,
        out: &mut CudaViewMut<f32>,
        heads: usize,
        kv_heads: usize,
        d: usize,
        kv_len: usize,
        scale: f32,
    ) -> Result<()> {
        let g = heads / kv_heads;
        // The selected positions as a list, so warps split them evenly however the
        // selection clusters.
        let mut sel = ws.take_bytes_at_least(self, "attn.sel", 4 * (kv_len + 1))?;
        let f = self.func("mask_compact")?;
        let n32 = kv_len as i32;
        let cfg = LaunchConfig {
            grid_dim: (1, 1, 1),
            block_dim: (1024, 1, 1),
            shared_mem_bytes: 0,
        };
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(mask)
                .arg(&mut sel)
                .arg(&n32)
                .launch(cfg)?
        };
        // About 16 selected keys per warp: a QSA selection keeps at most a few
        // thousand keys however long the context.
        const WARPS: usize = 8;
        let p = kv_len.min(4096).div_ceil(16).next_multiple_of(WARPS);
        let blocks = p / WARPS;
        let mut part = ws.take(self, "attn.fd_part", kv_heads * p * g * (d + 2))?;
        let f = self.func("attn_decode_partial_g12")?;
        let (h32, kvh32) = (heads as i32, kv_heads as i32);
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
                .arg(&sel)
                .arg(&mut part)
                .arg(&kvh32)
                .arg(&scale)
                .arg(&(kb as i32))
                .arg(&(vb as i32))
                .arg(&levels)
                .launch(cfg)?
        };
        ws.give_bytes("attn.sel", sel);
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
    /// Device address of the KV codebooks for `format` (0 for fp32, which reads none),
    /// uploading them on first use.
    fn kv_codebooks(&self, format: KvFormat) -> Result<u64> {
        if format == KvFormat::F32 {
            return Ok(0);
        }
        let mut table = self.kv_codebooks.lock().unwrap();
        if table.is_none() {
            *table = Some(self.upload_f32(KvFormat::codebooks())?);
        }
        let buf = table.as_ref().expect("uploaded above");
        Ok(buf.0.device_ptr(&self.stream).0)
    }

    /// Rotates `rows` rows of `d` coordinates of `x` in place into (or, `inverse`,
    /// out of) the space of a Turbo KV cache.
    fn kv_rotate(&self, x: &mut Buf, rows: usize, d: usize, inverse: bool) -> Result<()> {
        self.check(kv_row_shape(d, 4) && x.len() >= rows * d, "kv_rotate sizes")?;
        let f = self.func("kv_rotate")?;
        let (d32, inv) = (d as i32, i32::from(inverse));
        let cfg = LaunchConfig {
            grid_dim: (rows as u32, 1, 1),
            block_dim: (d as u32, 1, 1),
            shared_mem_bytes: 0,
        };
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(x)
                .arg(&d32)
                .arg(&inv)
                .launch(cfg)?
        };
        Ok(())
    }

    /// Masked GQA attention of `t` queries over the first `kv_len` cache rows with an
    /// online softmax (no `[t, heads, kv]` score matrix). Needs `d <= 256`. Each
    /// block's key tiles are gathered from the positions its tokens may see (the
    /// workspace's `"attn.sel_prefill"`, about `t * kv_len` bytes).
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn attn_prefill(
        &self,
        ws: &mut Workspace<Gpu>,
        q: &Buf,
        k: &Dev<u8>,
        v: &Dev<u8>,
        (kb, vb, levels): (u8, u8, u64),
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
        let groups = t.div_ceil(tq);
        let sel_stride = kv_len + 1;
        let mut sel = ws.take_bytes_at_least(self, "attn.sel_prefill", 4 * groups * sel_stride)?;
        let (t32, tq32, n32, stride32, sel32) = (
            t as i32,
            tq as i32,
            kv_len as i32,
            kv_stride as i32,
            sel_stride as i32,
        );
        let fu = self.func("mask_union_compact")?;
        let cfg = LaunchConfig {
            grid_dim: (groups as u32, 1, 1),
            block_dim: (1024, 1, 1),
            shared_mem_bytes: 0,
        };
        unsafe {
            self.stream
                .launch_builder(&fu)
                .arg(mask)
                .arg(&mut sel)
                .arg(&t32)
                .arg(&tq32)
                .arg(&n32)
                .arg(&stride32)
                .arg(&sel32)
                .launch(cfg)?
        };
        let a = [t, heads, kv_heads, d, kv_len, kv_stride].map(|v| v as i32);
        let cfg = LaunchConfig {
            grid_dim: (groups as u32, kv_heads as u32, 1),
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
                .arg(&(kb as i32))
                .arg(&(vb as i32))
                .arg(&levels)
                .arg(&sel)
                .arg(&sel32)
                .launch(cfg)?
        };
        ws.give_bytes("attn.sel_prefill", sel);
        Ok(())
    }
}
