//! Elementwise kernels and data movement.

use anyhow::Result;
use cudarc::driver::PushKernelArg;
use oominf_core::Elementwise;

use crate::{Buf, Gpu, grid};

impl Elementwise for Gpu {
    /// `out = x + y`.
    fn add(&self, x: &Buf, y: &Buf, out: &mut Buf, n: usize) -> Result<()> {
        let f = self.func("add_out")?;
        let n32 = n as i32;
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(x)
                .arg(y)
                .arg(out)
                .arg(&n32)
                .launch(grid(n, 256))?
        };
        Ok(())
    }

    fn silu_scale(&self, x: &Buf, y: &mut Buf, scale: f32, n: usize) -> Result<()> {
        let f = self.func("silu_scale")?;
        let n32 = n as i32;
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(x)
                .arg(y)
                .arg(&scale)
                .arg(&n32)
                .launch(grid(n, 256))?
        };
        Ok(())
    }

    fn silu_mul(&self, gate: &Buf, up: &Buf, y: &mut Buf, n: usize) -> Result<()> {
        let f = self.func("silu_mul")?;
        let n32 = n as i32;
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(gate)
                .arg(up)
                .arg(y)
                .arg(&n32)
                .launch(grid(n, 256))?
        };
        Ok(())
    }

    fn silu_mul_rows(&self, gu: &Buf, y: &mut Buf, rows: usize, n: usize) -> Result<()> {
        self.check(
            gu.len() >= rows * 2 * n && y.len() >= rows * n,
            "silu_mul_rows sizes",
        )?;
        let f = self.func("silu_mul_rows")?;
        let (r32, n32) = (rows as i32, n as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(gu)
                .arg(y)
                .arg(&r32)
                .arg(&n32)
                .launch(grid(rows * n, 256))?
        };
        Ok(())
    }

    /// `x *= sigmoid(gate)`.
    fn mul_sigmoid(&self, x: &mut Buf, gate: &Buf, n: usize) -> Result<()> {
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

    /// Copies rows `[first, first + n)` of a `[_, h]` matrix into `dst`.
    fn copy_rows(&self, src: &Buf, first: usize, n: usize, h: usize, dst: &mut Buf) -> Result<()> {
        self.check(
            src.len() >= (first + n) * h && dst.len() >= n * h,
            "copy_rows sizes",
        )?;
        let f = self.func("copy_rows")?;
        let (f32_, n32, h32) = (first as i32, n as i32, h as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(src)
                .arg(dst)
                .arg(&f32_)
                .arg(&n32)
                .arg(&h32)
                .launch(grid(n * h, 256))?
        };
        Ok(())
    }

    /// `dst[rows, cols] = src[:, col .. col + cols]` for `src` rows of `stride`.
    fn copy_cols(
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

    /// `dst[r, col .. col + cols] = src[r, :]` for `rows` rows of `stride` floats.
    fn put_cols(
        &self,
        src: &Buf,
        dst: &mut Buf,
        rows: usize,
        stride: usize,
        col: usize,
        cols: usize,
    ) -> Result<()> {
        self.check(
            src.len() >= rows * cols && dst.len() >= (rows - 1) * stride + col + cols,
            "put_cols sizes",
        )?;
        let f = self.func("put_cols")?;
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

    /// `dst[offset .. offset + n] = src[..n]`.
    fn copy_at(&self, src: &Buf, dst: &mut Buf, offset: usize, n: usize) -> Result<()> {
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

    /// `dst[dst_off..dst_off + n] = src[src_off..src_off + n]`.
    fn copy_range(
        &self,
        src: &Buf,
        src_off: usize,
        dst: &mut Buf,
        dst_off: usize,
        n: usize,
    ) -> Result<()> {
        self.check(
            src.len() >= src_off + n && dst.len() >= dst_off + n,
            "copy_range sizes",
        )?;
        if n == 0 {
            return Ok(());
        }
        let f = self.func("copy_range")?;
        let n32 = n as i32;
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(src)
                .arg(&src_off)
                .arg(dst)
                .arg(&dst_off)
                .arg(&n32)
                .launch(grid(n, 256))?
        };
        Ok(())
    }

    /// `dst[b, a, :] = src[a, b, :]` for `src` `[a_len, b_len, d]`.
    fn swap01(&self, src: &Buf, dst: &mut Buf, a_len: usize, b_len: usize, d: usize) -> Result<()> {
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

    /// Mean of each group of `ratio` consecutive rows of width `d`.
    fn pool_rows(
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
}
