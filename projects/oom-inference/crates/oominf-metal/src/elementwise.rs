use super::Gpu;
use anyhow::{Result, ensure};
use oominf_core::{Elementwise, Memory};

impl Gpu {
    #[allow(clippy::too_many_arguments)]
    fn element(
        &self,
        op: u32,
        a: &super::Dev<f32>,
        b: &super::Dev<f32>,
        out: &super::Dev<f32>,
        n: usize,
        width: usize,
        scale: f32,
    ) -> Result<()> {
        ensure!(a.len >= n && out.len >= n, "elementwise buffer too small");
        self.launch(
            "elementwise",
            &[a.buffer(), b.buffer(), out.buffer()],
            &[op, n as u32, width as u32, scale.to_bits()],
            n,
            128,
        )
    }
}

impl Elementwise for Gpu {
    fn add(&self, x: &Self::F32, y: &Self::F32, out: &mut Self::F32, n: usize) -> Result<()> {
        ensure!(y.len >= n, "add buffer too small");
        self.element(0, x, y, out, n, 0, 1.)
    }
    fn silu_scale(&self, x: &Self::F32, y: &mut Self::F32, scale: f32, n: usize) -> Result<()> {
        self.element(1, x, x, y, n, 0, scale)
    }
    fn silu_mul(
        &self,
        gate: &Self::F32,
        up: &Self::F32,
        y: &mut Self::F32,
        n: usize,
    ) -> Result<()> {
        ensure!(up.len >= n, "SiLU buffer too small");
        self.element(2, gate, up, y, n, 0, 1.)
    }
    fn silu_mul_rows(
        &self,
        gu: &Self::F32,
        y: &mut Self::F32,
        rows: usize,
        n: usize,
    ) -> Result<()> {
        ensure!(gu.len >= rows * 2 * n, "stacked SiLU buffer too small");
        self.element(3, gu, gu, y, rows * n, n, 1.)
    }
    fn mul_sigmoid(&self, x: &mut Self::F32, gate: &Self::F32, n: usize) -> Result<()> {
        ensure!(gate.len >= n, "gate buffer too small");
        self.element(4, x, gate, x, n, 0, 1.)
    }
    fn copy_rows(
        &self,
        src: &Self::F32,
        first: usize,
        n: usize,
        h: usize,
        dst: &mut Self::F32,
    ) -> Result<()> {
        self.copy_range(src, first * h, dst, 0, n * h)
    }
    fn copy_cols(
        &self,
        src: &Self::F32,
        dst: &mut Self::F32,
        rows: usize,
        stride: usize,
        col: usize,
        cols: usize,
    ) -> Result<()> {
        ensure!(
            col + cols <= stride && src.len >= rows * stride && dst.len >= rows * cols,
            "column copy out of bounds"
        );
        self.launch(
            "copy_columns",
            &[src.buffer(), dst.buffer()],
            &[0, rows as u32, stride as u32, col as u32, cols as u32],
            rows * cols,
            128,
        )
    }
    fn put_cols(
        &self,
        src: &Self::F32,
        dst: &mut Self::F32,
        rows: usize,
        stride: usize,
        col: usize,
        cols: usize,
    ) -> Result<()> {
        ensure!(
            col + cols <= stride && dst.len >= rows * stride && src.len >= rows * cols,
            "column write out of bounds"
        );
        self.launch(
            "copy_columns",
            &[src.buffer(), dst.buffer()],
            &[1, rows as u32, stride as u32, col as u32, cols as u32],
            rows * cols,
            128,
        )
    }
    fn copy_at(&self, src: &Self::F32, dst: &mut Self::F32, offset: usize, n: usize) -> Result<()> {
        self.copy_range(src, 0, dst, offset, n)
    }
    fn copy_range(
        &self,
        src: &Self::F32,
        src_off: usize,
        dst: &mut Self::F32,
        dst_off: usize,
        n: usize,
    ) -> Result<()> {
        ensure!(
            src_off <= src.len
                && n <= src.len - src_off
                && dst_off <= dst.len
                && n <= dst.len - dst_off,
            "float copy out of bounds"
        );
        self.finish()?;
        // SAFETY: checked ranges in completed GPU buffers; source and destination may overlap.
        unsafe {
            std::ptr::copy(
                src.buffer().contents().cast::<f32>().add(src_off),
                dst.buffer().contents().cast::<f32>().add(dst_off),
                n,
            )
        };
        Ok(())
    }
    fn swap01(
        &self,
        src: &Self::F32,
        dst: &mut Self::F32,
        a_len: usize,
        b_len: usize,
        d: usize,
    ) -> Result<()> {
        let source = self.download_f32(src)?;
        ensure!(
            source.len() >= a_len * b_len * d && dst.len >= a_len * b_len * d,
            "transpose buffer too small"
        );
        let mut target = vec![0.; a_len * b_len * d];
        for a in 0..a_len {
            for b in 0..b_len {
                target[(b * a_len + a) * d..(b * a_len + a + 1) * d]
                    .copy_from_slice(&source[(a * b_len + b) * d..(a * b_len + b + 1) * d]);
            }
        }
        self.write(&target, dst, 0)
    }
    fn pool_rows(
        &self,
        raw: &Self::F32,
        out: &mut Self::F32,
        nblocks: usize,
        ratio: usize,
        d: usize,
    ) -> Result<()> {
        ensure!(
            ratio > 0 && raw.len >= nblocks * ratio * d && out.len >= nblocks * d,
            "pool buffer too small"
        );
        let source = self.download_f32(raw)?;
        let mut target = vec![0.; nblocks * d];
        for block in 0..nblocks {
            for c in 0..d {
                target[block * d + c] = (0..ratio)
                    .map(|r| source[(block * ratio + r) * d + c])
                    .sum::<f32>()
                    / ratio as f32;
            }
        }
        self.write(&target, out, 0)
    }
}
