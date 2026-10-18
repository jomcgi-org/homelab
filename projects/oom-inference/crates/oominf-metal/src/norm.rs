use super::Gpu;
use anyhow::{Result, ensure};
use oominf_core::Norm;

impl Norm for Gpu {
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
    ) -> Result<()> {
        ensure!(
            group > 0
                && row.is_multiple_of(group)
                && x.len >= rows * row
                && out.len >= rows * row
                && w.len >= group,
            "RMSNorm buffer or geometry invalid"
        );
        self.launch(
            "rmsnorm",
            &[x.buffer(), w.buffer(), out.buffer()],
            &[group as u32, eps.to_bits(), plus_one.to_bits()],
            rows * row / group * 32,
            128,
        )
    }
    fn l2norm_heads(
        &self,
        x: &mut Self::F32,
        t: usize,
        stride: usize,
        offset: usize,
        nheads: usize,
        d: usize,
        eps: f32,
    ) -> Result<()> {
        ensure!(
            d > 0 && offset + nheads * d <= stride && x.len >= t * stride,
            "L2 norm geometry invalid"
        );
        self.launch(
            "l2norm",
            &[x.buffer()],
            &[
                stride as u32,
                offset as u32,
                nheads as u32,
                d as u32,
                eps.to_bits(),
            ],
            t * nheads * 32,
            128,
        )
    }
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
    ) -> Result<()> {
        ensure!(
            d > 0
                && per_token > 0
                && x.len >= rows * d
                && out.len >= rows * d
                && w.len >= d
                && z.len >= rows.div_ceil(per_token) * z_stride
                && z_off + per_token * d <= z_stride,
            "gated norm geometry invalid"
        );
        self.launch(
            "gated_norm",
            &[x.buffer(), z.buffer(), w.buffer(), out.buffer()],
            &[
                z_off as u32,
                z_stride as u32,
                per_token as u32,
                d as u32,
                eps.to_bits(),
            ],
            rows * 32,
            128,
        )
    }
}
