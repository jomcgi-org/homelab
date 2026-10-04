//! Normalisations (fp32).

use anyhow::Result;
use cudarc::driver::{LaunchConfig, PushKernelArg};
use oominf_core::Norm;

use crate::{Bf16Buf, Buf, Gpu, grid};

impl Norm for Gpu {
    /// RMSNorm over groups of `group` within rows of `row` elements;
    /// `plus_one` = 1.0 for `(1 + w)` weights, 0.0 for plain `w`.
    #[allow(clippy::too_many_arguments)]
    fn rmsnorm_groups(
        &self,
        x: &Buf,
        w: &Bf16Buf,
        out: &mut Buf,
        rows: usize,
        row: usize,
        group: usize,
        eps: f32,
        plus_one: f32,
    ) -> Result<()> {
        let f = self.func("rmsnorm_groups")?;
        let (g, r) = (group as i32, row as i32);
        let cfg = LaunchConfig {
            grid_dim: ((rows * row / group) as u32, 1, 1),
            block_dim: (256, 1, 1),
            shared_mem_bytes: 0,
        };
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(x)
                .arg(w)
                .arg(out)
                .arg(&g)
                .arg(&r)
                .arg(&eps)
                .arg(&plus_one)
                .launch(cfg)?
        };
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn l2norm_heads(
        &self,
        x: &mut Buf,
        t: usize,
        stride: usize,
        offset: usize,
        nheads: usize,
        d: usize,
        eps: f32,
    ) -> Result<()> {
        let f = self.func("l2norm_heads")?;
        let (t32, s32, o32, n32, d32) = (
            t as i32,
            stride as i32,
            offset as i32,
            nheads as i32,
            d as i32,
        );
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(x)
                .arg(&t32)
                .arg(&s32)
                .arg(&o32)
                .arg(&n32)
                .arg(&d32)
                .arg(&eps)
                .launch(grid(t * nheads * 32, 256))?
        };
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    /// Rows `r = t * per_token + i` of `x` (`[rows, d]`) gated by `z` rows at
    /// `z + z_off + t * z_stride + i * d`.
    fn gated_rmsnorm_sigmoid(
        &self,
        x: &Buf,
        z: &Buf,
        z_off: usize,
        z_stride: usize,
        per_token: usize,
        w: &Bf16Buf,
        out: &mut Buf,
        rows: usize,
        d: usize,
        eps: f32,
    ) -> Result<()> {
        let tokens = rows / per_token;
        self.check(
            rows.is_multiple_of(per_token)
                && z.len() >= z_off + (tokens - 1) * z_stride + per_token * d,
            "gated_rmsnorm_sigmoid sizes",
        )?;
        let f = self.func("gated_rmsnorm_sigmoid")?;
        let zp = self.ptr_at(z, z_off);
        let (zs32, pt32, r32, d32) = (z_stride as i32, per_token as i32, rows as i32, d as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(x)
                .arg(&zp)
                .arg(&zs32)
                .arg(&pt32)
                .arg(w)
                .arg(out)
                .arg(&r32)
                .arg(&d32)
                .arg(&eps)
                .launch(grid(rows * 32, 256))?
        };
        Ok(())
    }
}
