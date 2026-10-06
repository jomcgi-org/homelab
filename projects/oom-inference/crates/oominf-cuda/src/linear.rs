//! Dense GEMM and GEMV with fp32 accumulation.

use anyhow::{Result, ensure};
use cudarc::cublas::sys as blas_sys;
use cudarc::driver::{DevicePtr, DevicePtrMut, LaunchConfig, PushKernelArg};
use oominf_core::{Linear, Memory};

use crate::{Bf16Buf, Buf, Dev, Gpu, grid};

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
        if t <= GEMV_MAX_TOKENS && k.is_multiple_of(8) {
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
    #[allow(clippy::too_many_arguments)]
    fn gemm_fp8(
        &self,
        x: &Buf,
        q: &Dev<u8>,
        scale: &Buf,
        y: &mut Buf,
        scratch: &mut Bf16Buf,
        t: usize,
        n: usize,
        k: usize,
    ) -> Result<()> {
        self.check(
            x.len() >= t * k
                && q.len() >= n * k
                && scale.len() >= n * k.div_ceil(oominf_core::fp8::BLOCK)
                && y.len() >= t * n,
            "gemm_fp8 sizes",
        )?;
        if t <= GEMV_MAX_TOKENS && k.is_multiple_of(16) {
            let (qp, _g) = q.device_ptr(&self.stream);
            return self.gemv(x, qp, Some(scale), y, t, n, k);
        }
        // Larger steps (prefill) run cuBLAS on bf16 weights, dequantized once and
        // kept: layer-major prefill reuses a layer's weights for every chunk.
        let key = q.device_ptr(&self.stream).0;
        let mut cache = self.fp8_cache.lock().unwrap();
        if !cache.iter().any(|(k2, _)| *k2 == key) {
            let mut w = self.uninit_bf16(n * k)?;
            let f = self.func("fp8_dequantize_blocks")?;
            let (n32, k32) = (n as i32, k as i32);
            unsafe {
                self.stream
                    .launch_builder(&f)
                    .arg(q)
                    .arg(scale)
                    .arg(&mut w)
                    .arg(&n32)
                    .arg(&k32)
                    .launch(grid(n * k, 256))?
            };
            cache.push_back((key, w));
            let mut bytes: usize = cache.iter().map(|(_, b)| b.len() * 2).sum();
            while bytes > FP8_CACHE_BYTES && cache.len() > 1 {
                let (_, old) = cache.pop_front().unwrap();
                bytes -= old.len() * 2;
            }
        } else {
            // Most recently used last.
            let pos = cache.iter().position(|(k2, _)| *k2 == key).unwrap();
            let e = cache.remove(pos).unwrap();
            cache.push_back(e);
        }
        let w = &cache.back().unwrap().1;
        self.gemm_bf16(x, w, y, scratch, t, n, k)
    }

    fn release_weight_cache(&self) {
        self.fp8_cache.lock().unwrap().clear();
    }
}

/// Bytes of bf16 weights [`Linear::gemm_fp8`] keeps dequantized: one layer's dense
/// weights (about 300 MB for Qwen 3.8 Flash) and then some.
pub(crate) const FP8_CACHE_BYTES: usize = 512 << 20;

/// Most tokens a dense projection runs as a GEMV (weights read once, no
/// dequantized copy): decode, and draft verification up to 16 tokens (a 32-token
/// instance spills registers: 8 GB/s).
const GEMV_MAX_TOKENS: usize = 16;

impl Gpu {
    /// Decode GEMV, `t <= 4`, `k % 8 == 0` (see `gemv_impl` in ops.cu).
    pub(crate) fn gemv_bf16(
        &self,
        x: &Buf,
        w: &Bf16Buf,
        y: &mut Buf,
        t: usize,
        n: usize,
        k: usize,
    ) -> Result<()> {
        let (wp, _g) = w.device_ptr(&self.stream);
        self.gemv(x, wp, None, y, t, n, k)
    }

    /// The decode GEMV over bf16 weights at `w` or, with `scale`, FP8 e4m3 weights
    /// scaled per 128-weight block (then `k % 16 == 0`).
    #[allow(clippy::too_many_arguments)]
    fn gemv(
        &self,
        x: &Buf,
        w: u64,
        scale: Option<&Buf>,
        y: &mut Buf,
        t: usize,
        n: usize,
        k: usize,
    ) -> Result<()> {
        ensure!(t <= GEMV_MAX_TOKENS, "gemv of {t} tokens");
        let (xp, _gx) = x.0.device_ptr(&self.stream);
        let (yp, _gy) = y.0.device_ptr_mut(&self.stream);
        // FP8 runs at most 8 tokens a launch: its 16-token instance spills registers
        // (5 KB per thread, 200x slower). Tokens are independent rows.
        let per = if scale.is_some() { 8 } else { GEMV_MAX_TOKENS };
        let mut t0 = 0;
        while t0 < t {
            let tc = per.min(t - t0);
            self.gemv_at(
                xp + (t0 * k * 4) as u64,
                w,
                scale,
                yp + (t0 * n * 4) as u64,
                tc,
                n,
                k,
            )?;
            t0 += tc;
        }
        Ok(())
    }

    /// [`Self::gemv`] of `t` rows of activations at device address `x` into `t` rows
    /// of outputs at `y`.
    #[allow(clippy::too_many_arguments)]
    fn gemv_at(
        &self,
        x: u64,
        w: u64,
        scale: Option<&Buf>,
        y: u64,
        t: usize,
        n: usize,
        k: usize,
    ) -> Result<()> {
        const WARPS: usize = 8;
        const ROWS_PER_WARP: usize = 4;
        const SMEM: usize = 48 * 1024;
        // K per warp step: 32 lanes x one 16-byte load (8 bf16 or 16 e4m3).
        let step = if scale.is_some() { 512 } else { 256 };
        let row_groups = n.div_ceil(ROWS_PER_WARP);
        let kchunks = k.div_ceil(step);
        // Steps of more than 4 tokens (draft verification) pad to a wide kernel's
        // token count `tp`; its rows past `t` read zeros and are not written.
        let tp = match t {
            0..=4 => t,
            5..=8 => 8,
            _ => 16,
        };
        // Enough warps to saturate memory bandwidth, and x per split must fit in shared.
        let want = 2048usize.div_ceil(row_groups).clamp(1, kchunks);
        let smem_min = (tp * k * 4).div_ceil(SMEM);
        let splits0 = want.max(smem_min).min(kchunks);
        let klen = k.div_ceil(splits0).div_ceil(step) * step;
        let splits = k.div_ceil(klen);
        let name = match (scale.is_some(), tp) {
            (false, 1) => "gemv_bf16_t1",
            (false, 2) => "gemv_bf16_t2",
            (false, 3) => "gemv_bf16_t3",
            (false, 4) => "gemv_bf16_t4",
            (false, 8) => "gemv_bf16_w8",
            (false, _) => "gemv_bf16_w16",
            (true, 1) => "gemv_fp8_t1",
            (true, 2) => "gemv_fp8_t2",
            (true, 3) => "gemv_fp8_t3",
            (true, 4) => "gemv_fp8_t4",
            (true, 8) => "gemv_fp8_w8",
            (true, _) => "gemv_fp8_w16",
        };
        let f = self.func(name)?;
        let smem = tp * klen.min(k) * 4;
        // A wide kernel's tile can fill the default 48 KB exactly, which with the
        // kernel's static shared flag exceeds it: opt in to the size it uses.
        if tp > 4 {
            f.set_attribute(
                cudarc::driver::sys::CUfunction_attribute::CU_FUNC_ATTRIBUTE_MAX_DYNAMIC_SHARED_SIZE_BYTES,
                smem as i32,
            )?;
        }
        let cfg = LaunchConfig {
            grid_dim: (row_groups.div_ceil(WARPS) as u32, splits as u32, 1),
            block_dim: ((WARPS * 32) as u32, 1, 1),
            shared_mem_bytes: smem as u32,
        };
        let (n32, k32, kl32) = (n as i32, k as i32, klen as i32);
        let mut guard = self.gemv_partial.lock().unwrap();
        let mut tickets = self.gemv_tickets.lock().unwrap();
        let (pp, tkp) = if splits == 1 {
            (0u64, 0u64)
        } else {
            let need = splits * tp * n;
            if guard.as_ref().is_none_or(|b| b.len() < need) {
                *guard = Some(self.uninit(need)?);
            }
            let tiles = cfg.grid_dim.0 as usize;
            if tickets.as_ref().is_none_or(|b| b.len() < tiles) {
                *tickets = Some(self.stream.alloc_zeros::<u32>(tiles)?);
            }
            let p = guard.as_mut().unwrap().0.device_ptr_mut(&self.stream).0;
            let tk = tickets.as_mut().unwrap().device_ptr_mut(&self.stream).0;
            (p, tk)
        };
        let mut b = self.stream.launch_builder(&f);
        b.arg(&x).arg(&w);
        if let Some(sc) = scale {
            b.arg(sc);
        }
        let tn = t as i32;
        b.arg(&y).arg(&pp).arg(&tkp).arg(&n32).arg(&k32).arg(&kl32);
        if tp > 4 {
            b.arg(&tn);
        }
        // SAFETY: argument types match the kernel; the partials and tickets live in
        // the Gpu (stable addresses) and are guarded for the launch.
        unsafe { b.launch(cfg)? };
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
