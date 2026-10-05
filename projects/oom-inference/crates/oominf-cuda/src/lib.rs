//! CUDA implementation of the oominf [`Backend`](oominf_core::Backend): one device,
//! one compute stream, cuBLAS for prefill GEMMs and the kernels in `kernels/*.cu`.
//!
//! Each module implements one of the core operation traits. Every operation is
//! issued on the compute stream; host reads synchronise implicitly.

use std::collections::HashMap;
use std::ops::{Deref, DerefMut};
use std::sync::{Arc, Mutex};

use anyhow::{Result, ensure};
use cudarc::cublas::CudaBlas;
use cudarc::driver::{
    CudaContext, CudaFunction, CudaModule, CudaSlice, CudaStream, LaunchArgs, LaunchConfig,
    PushKernelArg,
};
use oominf_core::DeviceBuffer;

include!(concat!(env!("OUT_DIR"), "/kernels.rs"));

mod attention;
mod elementwise;
mod experts;
mod hyper;
mod linear;
mod lookup;
mod memory;
mod norm;
mod recurrent;
mod staging;

pub use memory::{CopyQueue, Event};

/// A device buffer of `T`.
pub struct Dev<T>(CudaSlice<T>);

impl<T> Deref for Dev<T> {
    type Target = CudaSlice<T>;
    fn deref(&self) -> &CudaSlice<T> {
        &self.0
    }
}

impl<T> DerefMut for Dev<T> {
    fn deref_mut(&mut self) -> &mut CudaSlice<T> {
        &mut self.0
    }
}

impl<T> DeviceBuffer for Dev<T> {
    fn len(&self) -> usize {
        self.0.len()
    }
}

// SAFETY: both forward to cudarc's own impls for the wrapped slice.
unsafe impl<'a, 'b: 'a, T> PushKernelArg<&'b Dev<T>> for LaunchArgs<'a> {
    #[inline(always)]
    fn arg(&mut self, arg: &'b Dev<T>) -> &mut Self {
        self.arg(&arg.0)
    }
}

unsafe impl<'a, 'b: 'a, T> PushKernelArg<&'b mut Dev<T>> for LaunchArgs<'a> {
    #[inline(always)]
    fn arg(&mut self, arg: &'b mut Dev<T>) -> &mut Self {
        self.arg(&mut arg.0)
    }
}

/// A cudarc view of a core [`View`](oominf_core::View) of a device buffer.
pub(crate) fn cview<'a, T>(v: &oominf_core::View<'a, Dev<T>>) -> cudarc::driver::CudaView<'a, T> {
    v.buf.0.slice(v.start..v.start + v.len)
}

pub type Buf = Dev<f32>;
pub type Bf16Buf = Dev<u16>;

pub struct Gpu {
    /// Pinned buffers for small uploads; first, so it drops before the context.
    staging: Mutex<staging::Staging>,
    ctx: Arc<CudaContext>,
    stream: Arc<CudaStream>,
    blas: CudaBlas,
    modules: Vec<Arc<CudaModule>>,
    /// Kernel handles by name, resolved once.
    funcs: Mutex<HashMap<String, CudaFunction>>,
    /// Split-K partials of the decode GEMV, grown on demand and reused (stable address).
    gemv_partial: Mutex<Option<Buf>>,
    /// One self-resetting ticket per GEMV row tile (all zero between launches).
    gemv_tickets: Mutex<Option<CudaSlice<u32>>>,
    /// [`oominf_core::KvFormat::codebooks`] on the device, uploaded on first use.
    kv_codebooks: Mutex<Option<Buf>>,
}

pub(crate) fn grid(n: usize, block: u32) -> LaunchConfig {
    LaunchConfig {
        grid_dim: ((n as u32).div_ceil(block).max(1), 1, 1),
        block_dim: (block, 1, 1),
        shared_mem_bytes: 0,
    }
}

impl Gpu {
    /// Opens device `ordinal` and loads every compiled kernel module.
    pub fn new(ordinal: usize) -> Result<Self> {
        let ctx = CudaContext::new(ordinal)?;
        // Ordering is explicit (one compute stream, events for the copy queues), so
        // cudarc's per-buffer event tracking is unnecessary overhead.
        unsafe { ctx.disable_event_tracking() };
        let stream = ctx.default_stream();
        let blas = CudaBlas::new(stream.clone())?;
        let modules = KERNELS
            .iter()
            .map(|(_, ptx)| ctx.load_module(cudarc::nvrtc::Ptx::from_src(*ptx)))
            .collect::<std::result::Result<Vec<_>, _>>()?;
        Ok(Gpu {
            staging: Mutex::new(staging::Staging::new(ctx.clone())),
            ctx,
            stream,
            blas,
            modules,
            funcs: Mutex::new(HashMap::new()),
            gemv_partial: Mutex::new(None),
            gemv_tickets: Mutex::new(None),
            kv_codebooks: Mutex::new(None),
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
        anyhow::bail!("no kernel named {name}")
    }

    pub(crate) fn check(&self, ok: bool, what: &str) -> Result<()> {
        ensure!(ok, "{what}: buffer too small or shape unsupported");
        Ok(())
    }

    /// Device address of element `off` of `buf` (valid while the buffer lives).
    pub(crate) fn ptr_at(&self, buf: &Buf, off: usize) -> u64 {
        use cudarc::driver::DevicePtr;
        buf.0.device_ptr(&self.stream).0 + (off * 4) as u64
    }
}

// `Gpu` implements every operation trait, so it is a complete `Backend`.
const _: fn() = || {
    fn backend<B: oominf_core::Backend>() {}
    backend::<Gpu>();
};
