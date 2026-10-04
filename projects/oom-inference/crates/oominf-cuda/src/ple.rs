//! Per-layer embedding (PLE) kernels: `impl Gpu` launch wrappers for `kernels/ple.cu`.

use cudarc::driver::{CudaSlice, LaunchConfig, PushKernelArg};

use crate::{Bf16Buf, Buf, Gpu, Result, grid};

impl Gpu {
    /// FP8 E4M3 bytes to fp32, times a per-table `scale`.
    pub fn fp8_dequant_scaled(
        &self,
        rows: &CudaSlice<u8>,
        scale: f32,
        out: &mut Buf,
        n: usize,
    ) -> Result<()> {
        self.check(
            rows.len() >= n && out.len() >= n,
            "fp8_dequant_scaled sizes",
        )?;
        let f = self.func("fp8_dequant_scaled")?;
        let n32 = n as i32;
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(rows)
                .arg(&scale)
                .arg(out)
                .arg(&n32)
                .launch(grid(n, 256))?
        };
        Ok(())
    }

    /// PLE key/query gate: `gated[t, c, :] = sigmoid(signed_sqrt(<key, query> / sqrt(h))) * value[t, :]`.
    #[allow(clippy::too_many_arguments)]
    pub fn ple_gate(
        &self,
        key: &Buf,
        query: &Buf,
        value: &Buf,
        gated: &mut Buf,
        t: usize,
        c: usize,
        h: usize,
    ) -> Result<()> {
        self.check(
            key.len() >= t * c * h && gated.len() >= t * c * h && value.len() >= t * h,
            "ple_gate sizes",
        )?;
        let f = self.func("ple_gate")?;
        let (c32, h32) = (c as i32, h as i32);
        let inv = 1.0 / (h as f32).sqrt();
        let cfg = LaunchConfig {
            grid_dim: ((t * c) as u32, 1, 1),
            block_dim: (256, 1, 1),
            shared_mem_bytes: 0,
        };
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(key)
                .arg(query)
                .arg(value)
                .arg(gated)
                .arg(&c32)
                .arg(&h32)
                .arg(&inv)
                .launch(cfg)?
        };
        Ok(())
    }

    /// `out = gated + silu(dilated_causal_conv(x))` per channel with a rolling
    /// `[d, (k - 1) * dil]` state, oldest first.
    #[allow(clippy::too_many_arguments)]
    pub fn dilated_conv_silu_add(
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
