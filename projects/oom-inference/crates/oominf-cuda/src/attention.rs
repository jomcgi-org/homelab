//! Full-attention (sparse indexer) kernels: `impl Gpu` launch wrappers for
//! `kernels/attention.cu`.

use cudarc::driver::{CudaSlice, LaunchConfig, PushKernelArg};

use crate::{Buf, Gpu, Result, grid};

impl Gpu {
    /// Splits `qg` `[t, heads, 2 * d]` into `q` `[t, heads, d]` and `gate` `[t, heads * d]`.
    pub fn split_q_gate(
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

    /// `dst[rows, cols] = src[:, col .. col + cols]` for `src` rows of `stride`.
    pub fn copy_cols(
        &self,
        src: &Buf,
        dst: &mut Buf,
        rows: usize,
        stride: usize,
        col: usize,
        cols: usize,
    ) -> Result<()> {
        let f = self.func("copy_cols")?;
        let (r32, s32, c32, n32) = (rows as i32, stride as i32, col as i32, cols as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(src)
                .arg(dst)
                .arg(&r32)
                .arg(&s32)
                .arg(&c32)
                .arg(&n32)
                .launch(grid(rows * cols, 256))?
        };
        Ok(())
    }

    /// Rotate-half RoPE on the first `rd` dims of `[ntok, nheads, d]` rows; token `i`
    /// sits at position `pos_base + i * pos_stride`. `inv_freq` has `rd / 2` entries.
    #[allow(clippy::too_many_arguments)]
    pub fn rope_rotate_half(
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

    /// Mean of each group of `ratio` consecutive rows of width `d`.
    pub fn pool_rows(
        &self,
        raw: &Buf,
        out: &mut Buf,
        nblocks: usize,
        ratio: usize,
        d: usize,
    ) -> Result<()> {
        let f = self.func("pool_rows")?;
        let (b32, r32, d32) = (nblocks as i32, ratio as i32, d as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(raw)
                .arg(out)
                .arg(&b32)
                .arg(&r32)
                .arg(&d32)
                .launch(grid(nblocks * d, 256))?
        };
        Ok(())
    }

    /// QSA block selection: writes `mask` `[t, kv_stride]` (1 = attendable).
    /// `scores` needs `t * (kv_stride / ratio + 1)` floats.
    #[allow(clippy::too_many_arguments)]
    pub fn qsa_select(
        &self,
        q: &Buf,
        block_keys: &Buf,
        scores: &mut Buf,
        mask: &mut CudaSlice<u8>,
        t: usize,
        start: usize,
        nheads: usize,
        head_dim: usize,
        ratio: usize,
        topk: usize,
        kv_stride: usize,
    ) -> Result<()> {
        self.check(
            scores.len() >= t * (kv_stride / ratio + 1)
                && mask.len() >= t * kv_stride
                && start + t <= kv_stride,
            "qsa_select sizes",
        )?;
        let f = self.func("qsa_select")?;
        let a = [t, start, nheads, head_dim, ratio, topk, kv_stride].map(|v| v as i32);
        let cfg = LaunchConfig {
            grid_dim: (t as u32, 1, 1),
            block_dim: (256, 1, 1),
            shared_mem_bytes: 0,
        };
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(q)
                .arg(block_keys)
                .arg(scores)
                .arg(mask)
                .arg(&a[0])
                .arg(&a[1])
                .arg(&a[2])
                .arg(&a[3])
                .arg(&a[4])
                .arg(&a[5])
                .arg(&a[6])
                .launch(cfg)?
        };
        Ok(())
    }

    /// Masked GQA attention over the first `kv_len` cache rows.
    /// `scores` needs `t * heads * kv_stride` floats.
    #[allow(clippy::too_many_arguments)]
    pub fn attn_masked(
        &self,
        q: &Buf,
        k: &Buf,
        v: &Buf,
        mask: &CudaSlice<u8>,
        scores: &mut Buf,
        out: &mut Buf,
        t: usize,
        heads: usize,
        kv_heads: usize,
        d: usize,
        kv_len: usize,
        kv_stride: usize,
        scale: f32,
    ) -> Result<()> {
        self.check(
            scores.len() >= t * heads * kv_stride
                && kv_len <= kv_stride
                && heads.is_multiple_of(kv_heads),
            "attn_masked sizes",
        )?;
        let f = self.func("attn_masked")?;
        let a = [t, heads, kv_heads, d, kv_len, kv_stride].map(|v| v as i32);
        let cfg = LaunchConfig {
            grid_dim: ((t * heads) as u32, 1, 1),
            block_dim: (256, 1, 1),
            shared_mem_bytes: 0,
        };
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(q)
                .arg(k)
                .arg(v)
                .arg(mask)
                .arg(scores)
                .arg(out)
                .arg(&a[0])
                .arg(&a[1])
                .arg(&a[2])
                .arg(&a[3])
                .arg(&a[4])
                .arg(&a[5])
                .arg(&scale)
                .launch(cfg)?
        };
        Ok(())
    }

    /// `x *= sigmoid(gate)`.
    pub fn mul_sigmoid(&self, x: &mut Buf, gate: &Buf, n: usize) -> Result<()> {
        let f = self.func("mul_sigmoid")?;
        let n32 = n as i32;
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(x)
                .arg(gate)
                .arg(&n32)
                .launch(grid(n, 256))?
        };
        Ok(())
    }

    /// `dst[offset .. offset + n] = src[..n]`.
    pub fn copy_at(&self, src: &Buf, dst: &mut Buf, offset: usize, n: usize) -> Result<()> {
        self.check(src.len() >= n && dst.len() >= offset + n, "copy_at sizes")?;
        let f = self.func("copy_at")?;
        let n32 = n as i32;
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(src)
                .arg(dst)
                .arg(&offset)
                .arg(&n32)
                .launch(grid(n, 256))?
        };
        Ok(())
    }

    /// `dst[b, a, :] = src[a, b, :]` for `src` `[a_len, b_len, d]`.
    pub fn swap01(
        &self,
        src: &Buf,
        dst: &mut Buf,
        a_len: usize,
        b_len: usize,
        d: usize,
    ) -> Result<()> {
        let n = a_len * b_len * d;
        self.check(src.len() >= n && dst.len() >= n, "swap01 sizes")?;
        let f = self.func("swap01")?;
        let (a32, b32, d32) = (a_len as i32, b_len as i32, d as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(src)
                .arg(dst)
                .arg(&a32)
                .arg(&b32)
                .arg(&d32)
                .launch(grid(n, 256))?
        };
        Ok(())
    }
}
