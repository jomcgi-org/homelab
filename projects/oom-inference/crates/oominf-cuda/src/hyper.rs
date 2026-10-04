//! Hyper-connection mixing kernels.

use anyhow::Result;
use cudarc::driver::PushKernelArg;
use oominf_core::HyperConnection;

use crate::{Buf, Gpu, grid};

impl HyperConnection for Gpu {
    /// Splits the stacked hyper-connection `[down | inject logits]` projection:
    /// `act = silu(down * inv_c)`, `inject = 2 * sigmoid(logit * inv_c)`.
    #[allow(clippy::too_many_arguments)]
    fn hc_post_down(
        &self,
        fused: &Buf,
        lr: usize,
        hc: usize,
        inv_c: f32,
        act: &mut Buf,
        inject: &mut Buf,
        t: usize,
    ) -> Result<()> {
        self.check(fused.len() >= t * (lr + hc), "hc_post_down sizes")?;
        let f = self.func("hc_post_down")?;
        let (s32, l32, h32, t32) = ((lr + hc) as i32, lr as i32, hc as i32, t as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(fused)
                .arg(&s32)
                .arg(&l32)
                .arg(&h32)
                .arg(&inv_c)
                .arg(act)
                .arg(inject)
                .arg(&t32)
                .launch(grid(t * (lr + hc), 256))?
        };
        Ok(())
    }

    fn hc_mix(
        &self,
        up: &Buf,
        normed: &Buf,
        mixed: &mut Buf,
        t: usize,
        c: usize,
        h: usize,
    ) -> Result<()> {
        let f = self.func("hc_mix")?;
        let (t32, c32, h32) = (t as i32, c as i32, h as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(up)
                .arg(normed)
                .arg(mixed)
                .arg(&t32)
                .arg(&c32)
                .arg(&h32)
                .launch(grid(t * h, 256))?
        };
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn hc_combine(
        &self,
        res: &Buf,
        y: &Buf,
        inj: &Buf,
        out: &mut Buf,
        t: usize,
        c: usize,
        h: usize,
    ) -> Result<()> {
        let f = self.func("hc_combine")?;
        let (t32, c32, h32) = (t as i32, c as i32, h as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(res)
                .arg(y)
                .arg(inj)
                .arg(out)
                .arg(&t32)
                .arg(&c32)
                .arg(&h32)
                .launch(grid(t * c * h, 256))?
        };
        Ok(())
    }
}
