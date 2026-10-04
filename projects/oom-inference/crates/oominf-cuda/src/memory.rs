//! Device memory and asynchronous copies.

use std::sync::Arc;

use anyhow::{Result, ensure};
use cudarc::driver::{CudaEvent, CudaStream, DevicePtr, sys};
use oominf_core::{Memory, Transfer};

use crate::{Bf16Buf, Buf, Dev, Gpu};

/// A CUDA stream used only for copies.
pub struct CopyQueue(Arc<CudaStream>);

pub struct Event(CudaEvent);

const NO_TIMING: Option<sys::CUevent_flags> = Some(sys::CUevent_flags::CU_EVENT_DISABLE_TIMING);

impl Memory for Gpu {
    type F32 = Buf;
    type Bf16 = Bf16Buf;
    type Bytes = Dev<u8>;
    type I32 = Dev<i32>;
    type U64 = Dev<u64>;

    fn uninit(&self, n: usize) -> Result<Buf> {
        // SAFETY: callers only hand this to operations that write every element
        // before anything reads it.
        Ok(Dev(unsafe { self.stream.alloc::<f32>(n.max(1))? }))
    }

    fn zeros(&self, n: usize) -> Result<Buf> {
        Ok(Dev(self.stream.alloc_zeros::<f32>(n)?))
    }

    fn fill_zero(&self, buf: &mut Buf) -> Result<()> {
        Ok(self.stream.memset_zeros(&mut buf.0)?)
    }

    fn upload_f32(&self, host: &[f32]) -> Result<Buf> {
        Ok(Dev(self.stream.clone_htod(host)?))
    }

    fn upload_into(&self, host: &[f32], dst: &mut Buf) -> Result<()> {
        ensure!(host.len() == dst.0.len(), "upload_into length");
        Ok(self.stream.memcpy_htod(host, &mut dst.0)?)
    }

    fn write_f32_at(&self, host: &[f32], dst: &mut Buf, offset: usize) -> Result<()> {
        ensure!(
            offset + host.len() <= dst.0.len(),
            "write_f32_at: {} values at {offset} into {}",
            host.len(),
            dst.0.len()
        );
        Ok(self
            .stream
            .memcpy_htod(host, &mut dst.0.slice_mut(offset..offset + host.len()))?)
    }

    fn download_f32(&self, buf: &Buf) -> Result<Vec<f32>> {
        Ok(self.stream.clone_dtoh(&buf.0)?)
    }

    fn upload_bf16(&self, host: &[u16]) -> Result<Bf16Buf> {
        Ok(Dev(self.stream.clone_htod(host)?))
    }

    fn uninit_bf16(&self, n: usize) -> Result<Bf16Buf> {
        // SAFETY: as for `uninit`.
        Ok(Dev(unsafe { self.stream.alloc::<u16>(n.max(1))? }))
    }

    fn upload_bytes(&self, host: &[u8]) -> Result<Dev<u8>> {
        Ok(Dev(self.stream.clone_htod(host)?))
    }

    fn uninit_bytes(&self, n: usize) -> Result<Dev<u8>> {
        // SAFETY: as for `uninit`.
        Ok(Dev(unsafe { self.stream.alloc::<u8>(n.max(1))? }))
    }

    fn zeros_bytes(&self, n: usize) -> Result<Dev<u8>> {
        Ok(Dev(self.stream.alloc_zeros::<u8>(n)?))
    }

    fn download_bytes(&self, buf: &Dev<u8>) -> Result<Vec<u8>> {
        Ok(self.stream.clone_dtoh(&buf.0)?)
    }

    fn copy_bytes(
        &self,
        src: &Dev<u8>,
        src_off: usize,
        dst: &mut Dev<u8>,
        dst_off: usize,
        n: usize,
    ) -> Result<()> {
        self.check(
            src.len() >= src_off + n && dst.len() >= dst_off + n,
            "copy_bytes sizes",
        )?;
        if n > 0 {
            self.stream.memcpy_dtod(
                &src.0.slice(src_off..src_off + n),
                &mut dst.0.slice_mut(dst_off..dst_off + n),
            )?;
        }
        Ok(())
    }

    fn upload_i32(&self, host: &[i32]) -> Result<Dev<i32>> {
        Ok(Dev(self.stream.clone_htod(host)?))
    }

    fn download_i32(&self, buf: &Dev<i32>) -> Result<Vec<i32>> {
        Ok(self.stream.clone_dtoh(&buf.0)?)
    }

    fn write_i32(&self, host: &[i32], dst: &mut Dev<i32>) -> Result<()> {
        if dst.0.len() < host.len() {
            dst.0 = self
                .stream
                .alloc_zeros::<i32>(host.len().next_power_of_two())?;
        }
        Ok(self
            .stream
            .memcpy_htod(host, &mut dst.0.slice_mut(0..host.len()))?)
    }

    fn zeros_u64(&self, n: usize) -> Result<Dev<u64>> {
        Ok(Dev(self.stream.alloc_zeros::<u64>(n)?))
    }

    fn write_u64(&self, host: &[u64], dst: &mut Dev<u64>) -> Result<()> {
        if dst.0.len() < host.len() {
            dst.0 = self
                .stream
                .alloc_zeros::<u64>(host.len().next_power_of_two())?;
        }
        Ok(self
            .stream
            .memcpy_htod(host, &mut dst.0.slice_mut(0..host.len()))?)
    }

    fn bytes_addr(&self, buf: &Dev<u8>) -> u64 {
        buf.0.device_ptr(&self.stream).0
    }

    fn sync(&self) -> Result<()> {
        Ok(self.stream.synchronize()?)
    }

    fn mem_info(&self) -> Result<(usize, usize)> {
        // Freed buffers return to the stream-ordered allocator's pool, which keeps
        // them reserved; trim it so the free figure counts them (callers decide how
        // much memory other users may take from this).
        unsafe {
            use cudarc::driver::sys;
            self.ctx.bind_to_thread()?;
            let mut dev = 0;
            let mut pool = std::ptr::null_mut();
            if sys::cuCtxGetDevice(&mut dev) == sys::CUresult::CUDA_SUCCESS
                && sys::cuDeviceGetDefaultMemPool(&mut pool, dev) == sys::CUresult::CUDA_SUCCESS
            {
                self.stream.synchronize()?;
                sys::cuMemPoolTrimTo(pool, 0);
            }
        }
        Ok(self.ctx.mem_get_info()?)
    }
}

impl Transfer for Gpu {
    type CopyQueue = CopyQueue;
    type Event = Event;

    fn copy_queue(&self) -> Result<CopyQueue> {
        Ok(CopyQueue(self.ctx.new_stream()?))
    }

    unsafe fn pin_host(&self, ptr: *mut u8, len: usize) -> Result<()> {
        self.ctx.bind_to_thread()?;
        // SAFETY: the caller guarantees ptr/len stay valid until unpin_host.
        let r =
            unsafe { sys::cuMemHostRegister_v2(ptr.cast(), len, sys::CU_MEMHOSTREGISTER_PORTABLE) };
        ensure!(
            r == sys::cudaError_enum::CUDA_SUCCESS,
            "cuMemHostRegister of {len} bytes failed: {r:?}"
        );
        Ok(())
    }

    unsafe fn unpin_host(&self, ptr: *mut u8) {
        // SAFETY: ptr was registered by pin_host.
        unsafe {
            sys::cuMemHostUnregister(ptr.cast());
        }
    }

    unsafe fn copy_to_device(
        &self,
        queue: &CopyQueue,
        dst: u64,
        src: *const u8,
        len: usize,
    ) -> Result<()> {
        // SAFETY: the caller keeps the pinned source stable and the destination
        // unread until the copy completes.
        unsafe {
            let src = std::slice::from_raw_parts(src, len);
            cudarc::driver::result::memcpy_htod_async(dst, src, queue.0.cu_stream())?;
        }
        Ok(())
    }

    unsafe fn copy_on_device(
        &self,
        queue: &CopyQueue,
        dst: u64,
        src: u64,
        len: usize,
    ) -> Result<()> {
        // SAFETY: as for copy_to_device.
        unsafe {
            cudarc::driver::result::memcpy_dtod_async(dst, src, len, queue.0.cu_stream())?;
        }
        Ok(())
    }

    fn record_compute(&self) -> Result<Event> {
        Ok(Event(self.stream.record_event(NO_TIMING)?))
    }

    fn record_copies(&self, queue: &CopyQueue) -> Result<Event> {
        Ok(Event(queue.0.record_event(NO_TIMING)?))
    }

    fn copies_wait(&self, queue: &CopyQueue, event: &Event) -> Result<()> {
        Ok(queue.0.wait(&event.0)?)
    }

    fn compute_wait(&self, event: &Event) -> Result<()> {
        Ok(self.stream.wait(&event.0)?)
    }

    fn event_done(&self, event: &Event) -> Result<bool> {
        // SAFETY: querying a live event.
        let r = unsafe { sys::cuEventQuery(event.0.cu_event()) };
        Ok(r == sys::cudaError_enum::CUDA_SUCCESS)
    }

    fn event_wait(&self, event: &Event) -> Result<()> {
        Ok(event.0.synchronize()?)
    }

    fn copies_sync(&self, queue: &CopyQueue) -> Result<()> {
        Ok(queue.0.synchronize()?)
    }
}
