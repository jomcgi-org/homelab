//! Dense GEMM and GEMV with fp32 accumulation.

use anyhow::Result;
use cudarc::cublas::sys as blas_sys;
use cudarc::driver::{DevicePtr, DevicePtrMut, LaunchConfig, PushKernelArg};
use oominf_core::{Linear, Memory};

use crate::{Bf16Buf, Buf, Gpu, grid};

impl Linear for Gpu {
    /// `y[T, N] = x[T, K] @ w[N, K]^T` with bf16 weights and fp32 accumulation and
    /// output. Up to 4 tokens run as a bandwidth-bound GEMV on the fp32 activations
    /// directly; larger `t` rounds x to bf16 into `scratch` for a tensor-core GEMM.
    #[allow(clippy::too_many_arguments)]
    fn gemm_bf16(
        &self,
        x: &Buf,
        w: &Bf16Buf,
        y: &mut Buf,
        scratch: &mut Bf16Buf,
        t: usize,
        n: usize,
        k: usize,
    ) -> Result<()> {
        self.check(
            x.len() >= t * k && w.len() >= n * k && y.len() >= t * n,
            "gemm_bf16 sizes",
        )?;
        if t <= 4 && k.is_multiple_of(8) {
            return self.gemv_bf16(x, w, y, t, n, k);
        }
        self.check(scratch.len() >= t * k, "gemm_bf16 scratch")?;
        self.f32_to_bf16(x, scratch, t * k)?;
        let (wp, _g1) = w.device_ptr(&self.stream);
        let (xp, _g2) = scratch.device_ptr(&self.stream);
        let (yp, _g3) = y.device_ptr_mut(&self.stream);
        let (alpha, beta) = (1.0f32, 0.0f32);
        unsafe {
            cudarc::cublas::result::gemm_ex(
                *self.blas.handle(),
                blas_sys::cublasOperation_t::CUBLAS_OP_T,
                blas_sys::cublasOperation_t::CUBLAS_OP_N,
                n as i32,
                t as i32,
                k as i32,
                (&alpha) as *const f32 as *const _,
                wp as *const _,
                blas_sys::cudaDataType::CUDA_R_16BF,
                k as i32,
                xp as *const _,
                blas_sys::cudaDataType::CUDA_R_16BF,
                k as i32,
                (&beta) as *const f32 as *const _,
                yp as *mut _,
                blas_sys::cudaDataType::CUDA_R_32F,
                n as i32,
                blas_sys::cublasComputeType_t::CUBLAS_COMPUTE_32F,
                blas_sys::cublasGemmAlgo_t::CUBLAS_GEMM_DEFAULT,
            )?;
        }
        Ok(())
    }
}

impl Gpu {
    /// Decode GEMV, `t <= 4`, `k % 8 == 0` (see `gemv_bf16_impl` in ops.cu).
    pub(crate) fn gemv_bf16(
        &self,
        x: &Buf,
        w: &Bf16Buf,
        y: &mut Buf,
        t: usize,
        n: usize,
        k: usize,
    ) -> Result<()> {
        const WARPS: usize = 8;
        const ROWS_PER_WARP: usize = 4;
        const SMEM: usize = 48 * 1024;
        let row_groups = n.div_ceil(ROWS_PER_WARP);
        let kchunks = k.div_ceil(256);
        // Enough warps to saturate memory bandwidth, and x per split must fit in shared.
        let want = 2048usize.div_ceil(row_groups).clamp(1, kchunks);
        let smem_min = (t * k * 4).div_ceil(SMEM);
        let splits0 = want.max(smem_min).min(kchunks);
        let klen = k.div_ceil(splits0).div_ceil(256) * 256;
        let splits = k.div_ceil(klen);
        let name = [
            "gemv_bf16_t1",
            "gemv_bf16_t2",
            "gemv_bf16_t3",
            "gemv_bf16_t4",
        ][t - 1];
        let f = self.func(name)?;
        let cfg = LaunchConfig {
            grid_dim: (row_groups.div_ceil(WARPS) as u32, splits as u32, 1),
            block_dim: ((WARPS * 32) as u32, 1, 1),
            shared_mem_bytes: (t * klen.min(k) * 4) as u32,
        };
        let (n32, k32, kl32) = (n as i32, k as i32, klen as i32);
        if splits == 1 {
            let null: u64 = 0;
            unsafe {
                self.stream
                    .launch_builder(&f)
                    .arg(x)
                    .arg(w)
                    .arg(y)
                    .arg(&null)
                    .arg(&null)
                    .arg(&n32)
                    .arg(&k32)
                    .arg(&kl32)
                    .launch(cfg)?
            };
            return Ok(());
        }
        let mut guard = self.gemv_partial.lock().unwrap();
        let need = splits * t * n;
        if guard.as_ref().is_none_or(|b| b.len() < need) {
            *guard = Some(self.uninit(need)?);
        }
        let partial = guard.as_mut().unwrap();
        let mut tickets = self.gemv_tickets.lock().unwrap();
        let tiles = cfg.grid_dim.0 as usize;
        if tickets.as_ref().is_none_or(|b| b.len() < tiles) {
            *tickets = Some(self.stream.alloc_zeros::<u32>(tiles)?);
        }
        let tickets = tickets.as_mut().unwrap();
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(x)
                .arg(w)
                .arg(y)
                .arg(&mut *partial)
                .arg(&mut *tickets)
                .arg(&n32)
                .arg(&k32)
                .arg(&kl32)
                .launch(cfg)?
        };
        Ok(())
    }

    pub(crate) fn f32_to_bf16(&self, x: &Buf, y: &mut Bf16Buf, n: usize) -> Result<()> {
        let f = self.func("f32_to_bf16")?;
        let n32 = n as i32;
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(x)
                .arg(y)
                .arg(&n32)
                .launch(grid(n, 256))?
        };
        Ok(())
    }
}
