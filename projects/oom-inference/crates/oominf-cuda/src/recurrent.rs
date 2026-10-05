//! Gated DeltaNet and short-convolution kernels with carried state.

use anyhow::Result;
use cudarc::driver::{LaunchConfig, PushKernelArg};
use oominf_core::Recurrent;

use crate::{Bf16Buf, Buf, Gpu, grid};

impl Recurrent for Gpu {
    #[allow(clippy::too_many_arguments)]
    fn causal_conv_silu(
        &self,
        x: &Buf,
        x_off: usize,
        x_stride: usize,
        state: &mut Buf,
        w: &Bf16Buf,
        out: &mut Buf,
        t: usize,
        d: usize,
        k: usize,
    ) -> Result<()> {
        self.check(
            k <= 8 && x.len() >= x_off + (t - 1) * x_stride + d,
            "causal_conv_silu sizes",
        )?;
        let f = self.func("causal_conv_silu")?;
        let xp = self.ptr_at(x, x_off);
        let (s32, t32, d32, k32) = (x_stride as i32, t as i32, d as i32, k as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(&xp)
                .arg(&s32)
                .arg(&*state)
                .arg(w)
                .arg(out)
                .arg(&t32)
                .arg(&d32)
                .arg(&k32)
                .launch(grid(t * d, 256))?
        };
        let f = self.func("causal_conv_state")?;
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(&xp)
                .arg(&s32)
                .arg(state)
                .arg(&t32)
                .arg(&d32)
                .arg(&k32)
                .launch(grid(d, 128))?
        };
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    /// `a` and `b` are `[t, hv]` column ranges at `a_off` / `b_off` of rows of
    /// `ab_stride` floats in `ab`.
    fn gdn_gates(
        &self,
        ab: &Buf,
        a_off: usize,
        b_off: usize,
        ab_stride: usize,
        a_log: &Bf16Buf,
        dt_bias: &Bf16Buf,
        g: &mut Buf,
        beta: &mut Buf,
        t: usize,
        hv: usize,
    ) -> Result<()> {
        let last = (t - 1) * ab_stride + hv;
        self.check(
            ab.len() >= a_off + last && ab.len() >= b_off + last,
            "gdn_gates sizes",
        )?;
        let f = self.func("gdn_gates")?;
        let (ap, bp) = (self.ptr_at(ab, a_off), self.ptr_at(ab, b_off));
        let (s32, t32, h32) = (ab_stride as i32, t as i32, hv as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(&ap)
                .arg(&bp)
                .arg(&s32)
                .arg(a_log)
                .arg(dt_bias)
                .arg(g)
                .arg(beta)
                .arg(&t32)
                .arg(&h32)
                .launch(grid(t * hv, 256))?
        };
        Ok(())
    }

    /// Exact gated delta rule over `t` tokens; `dk` and `dv` must be 128.
    #[allow(clippy::too_many_arguments)]
    fn gdn_recurrent(
        &self,
        qkv: &Buf,
        g: &Buf,
        beta: &Buf,
        state: &mut Buf,
        out: &mut Buf,
        t: usize,
        stride: usize,
        hk: usize,
        hv: usize,
        dk: usize,
        dv: usize,
    ) -> Result<()> {
        self.check(dk == 128 && dv == 128, "gdn_recurrent needs Dk = Dv = 128")?;
        let f = self.func("gdn_recurrent")?;
        let scale = 1.0 / (dk as f32).sqrt();
        let (t32, s32, hk32, hv32, dv32) =
            (t as i32, stride as i32, hk as i32, hv as i32, dv as i32);
        // GDN_COLS value columns per block: Hv * Dv / GDN_COLS blocks fill the GPU
        // where one block per head left most SMs idle on a serial recurrence.
        let cfg = LaunchConfig {
            grid_dim: (hv as u32, (dv / GDN_COLS) as u32, 1),
            block_dim: (4 * GDN_COLS as u32, 1, 1),
            shared_mem_bytes: 0,
        };
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(qkv)
                .arg(g)
                .arg(beta)
                .arg(state)
                .arg(out)
                .arg(&t32)
                .arg(&s32)
                .arg(&hk32)
                .arg(&hv32)
                .arg(&dv32)
                .arg(&scale)
                .launch(cfg)?
        };
        Ok(())
    }

    /// `out = gated + silu(dilated_causal_conv(x))` per channel with a rolling
    /// `[d, (k - 1) * dil]` state, oldest first.
    #[allow(clippy::too_many_arguments)]
    fn dilated_conv_silu_add(
        &self,
        x: &Buf,
        state: &mut Buf,
        w: &Bf16Buf,
        gated: &Buf,
        out: &mut Buf,
        t: usize,
        d: usize,
        k: usize,
        dil: usize,
    ) -> Result<()> {
        let s = (k - 1) * dil;
        self.check(s <= 32, "dilated conv state longer than 32")?;
        self.check(
            state.len() >= d * s && w.len() >= d * k && out.len() >= t * d,
            "dilated_conv_silu_add sizes",
        )?;
        let f = self.func("dilated_conv_silu_add")?;
        let (t32, d32, k32, dil32) = (t as i32, d as i32, k as i32, dil as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(x)
                .arg(state)
                .arg(w)
                .arg(gated)
                .arg(out)
                .arg(&t32)
                .arg(&d32)
                .arg(&k32)
                .arg(&dil32)
                .launch(grid(d, 128))?
        };
        Ok(())
    }
}

/// Value columns per `gdn_recurrent` block (4 lanes each). Measured on a 32k
/// prefill: 16 beat 8, 32 and 128 (one block per head).
const GDN_COLS: usize = 16;
