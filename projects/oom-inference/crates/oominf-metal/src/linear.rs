use super::{Dev, Gpu};
use anyhow::{Result, ensure};
use oominf_core::Linear;

impl Linear for Gpu {
    fn gemm_bf16(
        &self,
        x: &Self::F32,
        w: &Self::Bf16,
        y: &mut Self::F32,
        _scratch: &mut Self::Bf16,
        t: usize,
        n: usize,
        k: usize,
    ) -> Result<()> {
        ensure!(
            x.len >= t * k && w.len >= n * k && y.len >= t * n,
            "bf16 GEMM buffer too small"
        );
        self.launch(
            "gemm_bf16",
            &[x.buffer(), w.buffer(), y.buffer()],
            &[t as u32, n as u32, k as u32],
            t * n * 32,
            128,
        )
    }
    fn gemm_fp8(
        &self,
        x: &Self::F32,
        q: &Self::Bytes,
        scale: &Self::F32,
        y: &mut Self::F32,
        _scratch: &mut Self::Bf16,
        t: usize,
        n: usize,
        k: usize,
    ) -> Result<()> {
        ensure!(
            x.len >= t * k && q.len >= n * k && scale.len >= n * k.div_ceil(128) && y.len >= t * n,
            "fp8 GEMM buffer too small"
        );
        self.launch(
            "gemm_fp8",
            &[x.buffer(), q.buffer(), scale.buffer(), y.buffer()],
            &[t as u32, n as u32, k as u32],
            t * n * 32,
            128,
        )
    }
}

impl Gpu {
    /// NVFP4 W4A32 matrix product, reading released packed weights and scales in place.
    #[allow(clippy::too_many_arguments)]
    pub fn gemm_nvfp4(
        &self,
        x: &Dev<f32>,
        q: &Dev<u8>,
        scale: &Dev<u8>,
        scale2: f32,
        y: &mut Dev<f32>,
        t: usize,
        n: usize,
        k: usize,
    ) -> Result<()> {
        ensure!(
            k.is_multiple_of(16),
            "NVFP4 columns must be divisible by 16"
        );
        ensure!(
            x.len >= t * k && q.len >= n * k / 2 && scale.len >= n * k / 16 && y.len >= t * n,
            "NVFP4 GEMM buffer too small"
        );
        self.launch(
            "gemm_nvfp4",
            &[x.buffer(), q.buffer(), scale.buffer(), y.buffer()],
            &[t as u32, n as u32, k as u32, scale2.to_bits()],
            t * n * 32,
            128,
        )
    }
}
