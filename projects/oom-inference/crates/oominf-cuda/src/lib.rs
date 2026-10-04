//! CUDA backend: one device, one stream, cuBLAS GEMMs and the reference kernels in
//! `kernels/ops.cu`.
//!
//! Buffers are fp32 activations ([`Buf`]), raw bf16 weights stored as `u16`
//! ([`Bf16Buf`]) and raw bytes. Every op is enqueued on the single stream; host
//! reads synchronise implicitly.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use cudarc::cublas::{CudaBlas, sys as blas_sys};
use cudarc::driver::{
    CudaContext, CudaFunction, CudaModule, CudaSlice, CudaStream, DevicePtr, DevicePtrMut,
    LaunchConfig, PushKernelArg,
};

pub use cudarc::driver::CudaSlice as Slice;

pub type Buf = CudaSlice<f32>;
pub type Bf16Buf = CudaSlice<u16>;

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("cuda driver: {0}")]
    Driver(#[from] cudarc::driver::DriverError),
    #[error("cublas: {0}")]
    Blas(#[from] cudarc::cublas::result::CublasError),
    #[error("{0}")]
    Usage(String),
}

pub type Result<T> = std::result::Result<T, Error>;

include!(concat!(env!("OUT_DIR"), "/kernels.rs"));

mod attention;
mod moe;
mod ple;

pub use moe::Nvfp4Record;

pub struct Gpu {
    pub ctx: Arc<CudaContext>,
    pub stream: Arc<CudaStream>,
    blas: CudaBlas,
    modules: Vec<Arc<CudaModule>>,
    /// Kernel handles by name, resolved once.
    funcs: Mutex<HashMap<String, CudaFunction>>,
    /// Split-K partials of the decode GEMV, grown on demand and reused (stable address).
    gemv_partial: Mutex<Option<Buf>>,
    /// One self-resetting ticket per GEMV row tile (all zero between launches).
    gemv_tickets: Mutex<Option<CudaSlice<u32>>>,
}

pub(crate) fn grid(n: usize, block: u32) -> LaunchConfig {
    LaunchConfig {
        grid_dim: ((n as u32).div_ceil(block).max(1), 1, 1),
        block_dim: (block, 1, 1),
        shared_mem_bytes: 0,
    }
}

impl Gpu {
    pub fn new(ordinal: usize) -> Result<Self> {
        let ctx = CudaContext::new(ordinal)?;
        // Single stream: cross-stream event tracking is unnecessary overhead.
        unsafe { ctx.disable_event_tracking() };
        let stream = ctx.default_stream();
        let blas = CudaBlas::new(stream.clone())?;
        let modules = KERNELS
            .iter()
            .map(|(_, ptx)| ctx.load_module(cudarc::nvrtc::Ptx::from_src(*ptx)))
            .collect::<std::result::Result<Vec<_>, _>>()?;
        Ok(Gpu {
            ctx,
            stream,
            blas,
            modules,
            funcs: Mutex::new(HashMap::new()),
            gemv_partial: Mutex::new(None),
            gemv_tickets: Mutex::new(None),
        })
    }

    /// Looks a kernel up by name across every compiled `kernels/*.cu` module (cached).
    pub(crate) fn func(&self, name: &str) -> Result<CudaFunction> {
        let mut cache = self.funcs.lock().unwrap();
        if let Some(f) = cache.get(name) {
            return Ok(f.clone());
        }
        for m in &self.modules {
            if let Ok(f) = m.load_function(name) {
                cache.insert(name.to_owned(), f.clone());
                return Ok(f);
            }
        }
        Err(Error::Usage(format!("no kernel named {name}")))
    }

    /// An fp32 buffer with unspecified contents, for outputs a kernel fully writes.
    pub fn uninit(&self, n: usize) -> Result<Buf> {
        // SAFETY: callers only hand this to kernels that overwrite every element
        // before anything reads it.
        Ok(unsafe { self.stream.alloc::<f32>(n.max(1))? })
    }

    pub fn zeros(&self, n: usize) -> Result<Buf> {
        Ok(self.stream.alloc_zeros::<f32>(n)?)
    }

    pub fn upload_f32(&self, host: &[f32]) -> Result<Buf> {
        Ok(self.stream.clone_htod(host)?)
    }

    /// Copies `host` into the existing device buffer `dst` (same length).
    pub fn upload_into(&self, host: &[f32], dst: &mut Buf) -> Result<()> {
        self.check(host.len() == dst.len(), "upload_into length")?;
        Ok(self.stream.memcpy_htod(host, dst)?)
    }

    pub fn upload_u16(&self, host: &[u16]) -> Result<Bf16Buf> {
        Ok(self.stream.clone_htod(host)?)
    }

    /// Splits the stacked hyper-connection `[down | inject logits]` projection:
    /// `act = silu(down * inv_c)`, `inject = 2 * sigmoid(logit * inv_c)`.
    #[allow(clippy::too_many_arguments)]
    pub fn hc_post_down(
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

    /// Raw device address of element `off` of `buf` (valid while the buffer lives).
    pub fn ptr_at(&self, buf: &Buf, off: usize) -> u64 {
        buf.device_ptr(&self.stream).0 + (off * 4) as u64
    }

    /// `dst[r, col .. col + cols] = src[r, :]` for `rows` rows of `stride` floats.
    pub fn put_cols(
        &self,
        src: &Buf,
        dst: &mut Buf,
        rows: usize,
        stride: usize,
        col: usize,
        cols: usize,
    ) -> Result<()> {
        self.check(
            src.len() >= rows * cols && dst.len() >= (rows - 1) * stride + col + cols,
            "put_cols sizes",
        )?;
        let f = self.func("put_cols")?;
        let (r32, s32, c32, n32) = (rows as i32, stride as i32, col as i32, cols as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(src)
                .arg(dst)
                .arg(&r32)
                .arg(&s32)
                .arg(&c32)
                .arg(&n32)
                .launch(grid(rows * cols, 256))?
        };
        Ok(())
    }

    /// Raw device address of a buffer (valid while the buffer lives).
    pub fn device_ptr<T>(&self, buf: &CudaSlice<T>) -> u64 {
        buf.device_ptr(&self.stream).0
    }

    pub fn upload_bytes(&self, host: &[u8]) -> Result<CudaSlice<u8>> {
        Ok(self.stream.clone_htod(host)?)
    }

    pub fn upload_i32(&self, host: &[i32]) -> Result<CudaSlice<i32>> {
        Ok(self.stream.clone_htod(host)?)
    }

    pub fn download<T: cudarc::driver::DeviceRepr + Default + Clone>(
        &self,
        buf: &CudaSlice<T>,
    ) -> Result<Vec<T>> {
        Ok(self.stream.clone_dtoh(buf)?)
    }

    pub fn sync(&self) -> Result<()> {
        Ok(self.stream.synchronize()?)
    }

    /// `y[T, N] = x[T, K] @ w[N, K]^T` with bf16 weights and fp32 accumulation and
    /// output. Up to 4 tokens run as a bandwidth-bound GEMV on the fp32 activations
    /// directly; larger `t` rounds x to bf16 into `scratch` for a tensor-core GEMM.
    #[allow(clippy::too_many_arguments)]
    pub fn gemm_bf16(
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

    /// Decode GEMV, `t <= 4`, `k % 8 == 0` (see `gemv_bf16_impl` in ops.cu).
    fn gemv_bf16(
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

    /// `y[T, N] = x[T, K] @ w[N, K]^T` entirely in fp32 (pedantic: no TF32).
    pub fn gemm_f32(
        &self,
        x: &Buf,
        w: &Buf,
        y: &mut Buf,
        t: usize,
        n: usize,
        k: usize,
    ) -> Result<()> {
        self.check(
            x.len() >= t * k && w.len() >= n * k && y.len() >= t * n,
            "gemm_f32 sizes",
        )?;
        let (wp, _g1) = w.device_ptr(&self.stream);
        let (xp, _g2) = x.device_ptr(&self.stream);
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
                blas_sys::cudaDataType::CUDA_R_32F,
                k as i32,
                xp as *const _,
                blas_sys::cudaDataType::CUDA_R_32F,
                k as i32,
                (&beta) as *const f32 as *const _,
                yp as *mut _,
                blas_sys::cudaDataType::CUDA_R_32F,
                n as i32,
                blas_sys::cublasComputeType_t::CUBLAS_COMPUTE_32F_PEDANTIC,
                blas_sys::cublasGemmAlgo_t::CUBLAS_GEMM_DEFAULT,
            )?;
        }
        Ok(())
    }

    pub(crate) fn check(&self, ok: bool, what: &str) -> Result<()> {
        if ok {
            Ok(())
        } else {
            Err(Error::Usage(format!("{what}: buffer too small")))
        }
    }

    pub fn f32_to_bf16(&self, x: &Buf, y: &mut Bf16Buf, n: usize) -> Result<()> {
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

    /// RMSNorm over groups of `group` within rows of `row` elements;
    /// `plus_one` = 1.0 for `(1 + w)` weights, 0.0 for plain `w`.
    #[allow(clippy::too_many_arguments)]
    pub fn rmsnorm_groups(
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

    pub fn silu_scale(&self, x: &Buf, y: &mut Buf, scale: f32, n: usize) -> Result<()> {
        let f = self.func("silu_scale")?;
        let n32 = n as i32;
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(x)
                .arg(y)
                .arg(&scale)
                .arg(&n32)
                .launch(grid(n, 256))?
        };
        Ok(())
    }

    pub fn hc_mix(
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

    pub fn hc_inject(&self, logit: &Buf, inj: &mut Buf, n: usize, inv_c: f32) -> Result<()> {
        let f = self.func("hc_inject")?;
        let n32 = n as i32;
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(logit)
                .arg(inj)
                .arg(&n32)
                .arg(&inv_c)
                .launch(grid(n, 256))?
        };
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    pub fn hc_combine(
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

    #[allow(clippy::too_many_arguments)]
    pub fn causal_conv_silu(
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
                .arg(state)
                .arg(w)
                .arg(out)
                .arg(&t32)
                .arg(&d32)
                .arg(&k32)
                .launch(grid(d, 128))?
        };
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    pub fn l2norm_heads(
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
    /// `a` and `b` are `[t, hv]` column ranges at `a_off` / `b_off` of rows of
    /// `ab_stride` floats in `ab`.
    pub fn gdn_gates(
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
    pub fn gdn_recurrent(
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
        let cfg = LaunchConfig {
            grid_dim: (hv as u32, 1, 1),
            block_dim: (4 * dv as u32, 1, 1),
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

    #[allow(clippy::too_many_arguments)]
    /// Rows `r = t * per_token + i` of `x` (`[rows, d]`) gated by `z` rows at
    /// `z + z_off + t * z_stride + i * d`.
    pub fn gated_rmsnorm_sigmoid(
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

    pub fn router_topk(
        &self,
        logits: &Buf,
        ids: &mut CudaSlice<i32>,
        weights: &mut Buf,
        t: usize,
        e: usize,
        k: usize,
    ) -> Result<()> {
        self.check(k <= 32 && e <= 8192, "router top-k > 32 or experts > 8192")?;
        let f = self.func("router_topk")?;
        let (e32, k32) = (e as i32, k as i32);
        let cfg = LaunchConfig {
            grid_dim: (t as u32, 1, 1),
            block_dim: (256, 1, 1),
            shared_mem_bytes: (e * 4) as u32,
        };
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(logits)
                .arg(ids)
                .arg(weights)
                .arg(&e32)
                .arg(&k32)
                .launch(cfg)?
        };
        Ok(())
    }

    pub fn silu_mul(&self, gate: &Buf, up: &Buf, y: &mut Buf, n: usize) -> Result<()> {
        let f = self.func("silu_mul")?;
        let n32 = n as i32;
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(gate)
                .arg(up)
                .arg(y)
                .arg(&n32)
                .launch(grid(n, 256))?
        };
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    pub fn moe_combine(
        &self,
        routed: &Buf,
        shared: &Buf,
        gate_logit: &Buf,
        out: &mut Buf,
        t: usize,
        h: usize,
    ) -> Result<()> {
        let f = self.func("moe_combine")?;
        let (t32, h32) = (t as i32, h as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(routed)
                .arg(shared)
                .arg(gate_logit)
                .arg(out)
                .arg(&t32)
                .arg(&h32)
                .launch(grid(t * h, 256))?
        };
        Ok(())
    }

    /// NVFP4 (ModelOpt, group 16) to fp32 from a device-resident expert record at raw
    /// device address `record` (parts at byte offsets, `scale2` read from the record).
    #[allow(clippy::too_many_arguments)]
    pub fn dequant_nvfp4(
        &self,
        record: u64,
        packed_off: usize,
        scale_off: usize,
        scale2_idx: usize,
        out: &mut Buf,
        rows: usize,
        cols: usize,
    ) -> Result<()> {
        self.check(
            cols.is_multiple_of(16) && out.len() >= rows * cols,
            "dequant_nvfp4 sizes",
        )?;
        let f = self.func("dequant_nvfp4")?;
        let (po, so, si) = (packed_off as i64, scale_off as i64, scale2_idx as i32);
        let (r32, c32) = (rows as i32, cols as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(&record)
                .arg(&po)
                .arg(&so)
                .arg(&si)
                .arg(out)
                .arg(&r32)
                .arg(&c32)
                .launch(grid(rows * cols, 256))?
        };
        Ok(())
    }

    pub fn gather_rows(
        &self,
        src: &Buf,
        idx: &CudaSlice<i32>,
        dst: &mut Buf,
        n: usize,
        h: usize,
    ) -> Result<()> {
        let f = self.func("gather_rows")?;
        let (n32, h32) = (n as i32, h as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(src)
                .arg(idx)
                .arg(dst)
                .arg(&n32)
                .arg(&h32)
                .launch(grid(n * h, 256))?
        };
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    pub fn scatter_add_weighted(
        &self,
        src: &Buf,
        idx: &CudaSlice<i32>,
        w: &Buf,
        dst: &mut Buf,
        n: usize,
        h: usize,
    ) -> Result<()> {
        let f = self.func("scatter_add_weighted")?;
        let (n32, h32) = (n as i32, h as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(src)
                .arg(idx)
                .arg(w)
                .arg(dst)
                .arg(&n32)
                .arg(&h32)
                .launch(grid(n * h, 256))?
        };
        Ok(())
    }

    /// `out = x + y`.
    pub fn add(&self, x: &Buf, y: &Buf, out: &mut Buf, n: usize) -> Result<()> {
        let f = self.func("add_out")?;
        let n32 = n as i32;
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(x)
                .arg(y)
                .arg(out)
                .arg(&n32)
                .launch(grid(n, 256))?
        };
        Ok(())
    }

    /// Copies rows `[first, first + n)` of a `[_, h]` matrix into `dst`.
    pub fn copy_rows(
        &self,
        src: &Buf,
        first: usize,
        n: usize,
        h: usize,
        dst: &mut Buf,
    ) -> Result<()> {
        self.check(
            src.len() >= (first + n) * h && dst.len() >= n * h,
            "copy_rows sizes",
        )?;
        let f = self.func("copy_rows")?;
        let (f32_, n32, h32) = (first as i32, n as i32, h as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(src)
                .arg(dst)
                .arg(&f32_)
                .arg(&n32)
                .arg(&h32)
                .launch(grid(n * h, 256))?
        };
        Ok(())
    }
}

/// Reusable named device buffers, so a steady-state decode step allocates nothing and
/// every intermediate keeps a stable address (a prerequisite for CUDA-graph capture).
///
/// Components `take` a buffer of an exact length and `give` it back when done. A
/// buffer of a different length is reallocated; one that is not given back is simply
/// reallocated next time. Contents are unspecified unless taken with `take_zeroed`.
#[derive(Default)]
pub struct Workspace {
    f32s: HashMap<&'static str, Buf>,
    bf16s: HashMap<&'static str, Bf16Buf>,
}

impl Workspace {
    pub fn new() -> Self {
        Self::default()
    }

    /// Device bytes held by buffers currently given back to the workspace.
    pub fn bytes(&self) -> usize {
        self.f32s.values().map(|b| b.len() * 4).sum::<usize>()
            + self.bf16s.values().map(|b| b.len() * 2).sum::<usize>()
    }

    pub fn take(&mut self, gpu: &Gpu, name: &'static str, n: usize) -> Result<Buf> {
        match self.f32s.remove(name) {
            Some(b) if b.len() == n.max(1) => Ok(b),
            _ => gpu.uninit(n),
        }
    }

    pub fn take_zeroed(&mut self, gpu: &Gpu, name: &'static str, n: usize) -> Result<Buf> {
        let mut b = self.take(gpu, name, n)?;
        gpu.stream.memset_zeros(&mut b)?;
        Ok(b)
    }

    pub fn give(&mut self, name: &'static str, buf: Buf) {
        self.f32s.insert(name, buf);
    }

    /// A bf16 scratch of at least `n` elements (grows, never shrinks).
    pub fn take_bf16(&mut self, gpu: &Gpu, name: &'static str, n: usize) -> Result<Bf16Buf> {
        match self.bf16s.remove(name) {
            Some(b) if b.len() >= n.max(1) => Ok(b),
            _ => Ok(unsafe { gpu.stream.alloc::<u16>(n.max(1))? }),
        }
    }

    pub fn give_bf16(&mut self, name: &'static str, buf: Bf16Buf) {
        self.bf16s.insert(name, buf);
    }
}
