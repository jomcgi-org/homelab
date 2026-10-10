//! Quantised embedding rows and per-stream gating: `kernels/ple.cu`.

use anyhow::Result;
use cudarc::driver::{LaunchConfig, PushKernelArg};
use oominf_core::Lookup;

use crate::{Buf, Dev, Gpu, grid};

impl Lookup for Gpu {
    /// FP8 E4M3 bytes to fp32, times a per-table `scale`.
    fn fp8_dequant_scaled(
        &self,
        rows: &Dev<u8>,
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
    fn ple_gate(
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
}
