//! CUDA backend: one device, one stream, cuBLAS GEMMs and the reference kernels in
//! `kernels/ops.cu`.
//!
//! Buffers are fp32 activations ([`Buf`]), raw bf16 weights stored as `u16`
//! ([`Bf16Buf`]) and raw bytes. Every op is enqueued on the single stream; host
//! reads synchronise implicitly.

use std::sync::Arc;

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

const PTX: &str = include_str!(concat!(env!("OUT_DIR"), "/ops.ptx"));

pub struct Gpu {
    pub ctx: Arc<CudaContext>,
    pub stream: Arc<CudaStream>,
    blas: CudaBlas,
    module: Arc<CudaModule>,
}

fn grid(n: usize, block: u32) -> LaunchConfig {
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
        let module = ctx.load_module(cudarc::nvrtc::Ptx::from_src(PTX))?;
        Ok(Gpu {
            ctx,
            stream,
            blas,
            module,
        })
    }

    fn func(&self, name: &str) -> Result<CudaFunction> {
        Ok(self.module.load_function(name)?)
    }

    pub fn zeros(&self, n: usize) -> Result<Buf> {
        Ok(self.stream.alloc_zeros::<f32>(n)?)
    }

    pub fn upload_f32(&self, host: &[f32]) -> Result<Buf> {
        Ok(self.stream.clone_htod(host)?)
    }

    pub fn upload_u16(&self, host: &[u16]) -> Result<Bf16Buf> {
        Ok(self.stream.clone_htod(host)?)
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

    /// `y[T, N] = x[T, K] @ w[N, K]^T` with bf16 operands (x rounded to bf16 into
    /// `scratch`) and fp32 accumulation and output.
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

    fn check(&self, ok: bool, what: &str) -> Result<()> {
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
        state: &mut Buf,
        w: &Bf16Buf,
        out: &mut Buf,
        t: usize,
        d: usize,
        k: usize,
    ) -> Result<()> {
        self.check(k <= 8, "conv kernel width > 8")?;
        let f = self.func("causal_conv_silu")?;
        let (t32, d32, k32) = (t as i32, d as i32, k as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(x)
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
    pub fn gdn_gates(
        &self,
        a: &Buf,
        b: &Buf,
        a_log: &Bf16Buf,
        dt_bias: &Bf16Buf,
        g: &mut Buf,
        beta: &mut Buf,
        t: usize,
        hv: usize,
    ) -> Result<()> {
        let f = self.func("gdn_gates")?;
        let (t32, h32) = (t as i32, hv as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(a)
                .arg(b)
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
            block_dim: (dv as u32, 1, 1),
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
    pub fn gated_rmsnorm_sigmoid(
        &self,
        x: &Buf,
        z: &Buf,
        w: &Bf16Buf,
        out: &mut Buf,
        rows: usize,
        d: usize,
        eps: f32,
    ) -> Result<()> {
        let f = self.func("gated_rmsnorm_sigmoid")?;
        let (r32, d32) = (rows as i32, d as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(x)
                .arg(z)
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
        self.check(k <= 32, "router top-k > 32")?;
        let f = self.func("router_topk")?;
        let (e32, k32) = (e as i32, k as i32);
        let cfg = LaunchConfig {
            grid_dim: (t as u32, 1, 1),
            block_dim: (256, 1, 1),
            shared_mem_bytes: 0,
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

    /// NVFP4 (ModelOpt, group 16) to fp32. `packed` and `scale` are byte views into a
    /// device-resident expert record.
    #[allow(clippy::too_many_arguments)]
    pub fn dequant_nvfp4(
        &self,
        record: &CudaSlice<u8>,
        packed_off: usize,
        scale_off: usize,
        scale2: f32,
        out: &mut Buf,
        rows: usize,
        cols: usize,
    ) -> Result<()> {
        self.check(
            cols.is_multiple_of(16) && out.len() >= rows * cols,
            "dequant_nvfp4 sizes",
        )?;
        let f = self.func("dequant_nvfp4")?;
        let packed = record.slice(packed_off..packed_off + rows * cols / 2);
        let scale = record.slice(scale_off..scale_off + rows * cols / 16);
        let (r32, c32) = (rows as i32, cols as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(&packed)
                .arg(&scale)
                .arg(&scale2)
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
}
