//! Ordered CPU copies between shared allocations. Events are complete on return;
//! asynchronous blits can replace these copies without changing the tier protocol.
use super::Gpu;
use anyhow::{Context, Result, ensure};
use oominf_core::{Memory, Transfer};

pub enum Download {
    I32(Vec<i32>),
    F32(Vec<f32>),
}

impl Gpu {
    fn address(
        &self,
        address: u64,
        len: usize,
    ) -> Result<(std::sync::Arc<super::Allocation>, usize)> {
        let addresses = self.allocations.lock().unwrap();
        let (base, weak) = addresses
            .range(..=address)
            .next_back()
            .context("unknown Metal address")?;
        let allocation = weak.upgrade().context("released Metal address")?;
        let offset = usize::try_from(address - base)?;
        ensure!(
            offset <= allocation.bytes && len <= allocation.bytes - offset,
            "Metal address range out of bounds"
        );
        Ok((allocation, offset))
    }
}

impl Transfer for Gpu {
    type CopyQueue = ();
    type Event = ();
    type Download = Download;
    fn copy_queue(&self) -> Result<()> {
        Ok(())
    }
    unsafe fn pin_host(&self, _ptr: *mut u8, _len: usize) -> Result<()> {
        Ok(())
    }
    unsafe fn unpin_host(&self, _ptr: *mut u8) {}
    unsafe fn copy_to_device(
        &self,
        _queue: &(),
        dst: u64,
        src: *const u8,
        len: usize,
    ) -> Result<()> {
        let (allocation, offset) = self.address(dst, len)?;
        self.finish()?;
        // SAFETY: the caller keeps src valid; address() checked the destination range.
        unsafe {
            std::ptr::copy_nonoverlapping(
                src,
                allocation.buffer.contents().cast::<u8>().add(offset),
                len,
            )
        };
        Ok(())
    }
    unsafe fn download_async(&self, src: &Self::I32, dst: *mut i32, n: usize) -> Result<()> {
        ensure!(n <= src.len, "download range out of bounds");
        self.finish()?;
        // SAFETY: the caller guarantees dst holds n elements; src is completed shared memory.
        unsafe { std::ptr::copy_nonoverlapping(src.buffer().contents().cast::<i32>(), dst, n) };
        Ok(())
    }
    fn download_start_i32(&self, src: &Self::I32, n: usize) -> Result<Download> {
        ensure!(n <= src.len, "download range out of bounds");
        Ok(Download::I32(self.download_i32(src)?[..n].to_vec()))
    }
    fn download_start_f32(&self, src: &Self::F32, n: usize) -> Result<Download> {
        ensure!(n <= src.len, "download range out of bounds");
        Ok(Download::F32(self.download_f32(src)?[..n].to_vec()))
    }
    fn download_wait_i32(&self, pending: Download) -> Result<Vec<i32>> {
        match pending {
            Download::I32(v) => Ok(v),
            _ => anyhow::bail!("expected an i32 download"),
        }
    }
    fn download_wait_f32(&self, pending: Download) -> Result<Vec<f32>> {
        match pending {
            Download::F32(v) => Ok(v),
            _ => anyhow::bail!("expected an f32 download"),
        }
    }
    unsafe fn copy_on_device(&self, _queue: &(), dst: u64, src: u64, len: usize) -> Result<()> {
        let (source, src_off) = self.address(src, len)?;
        let (destination, dst_off) = self.address(dst, len)?;
        self.finish()?;
        // SAFETY: both ranges have been checked, GPU commands are complete; overlap is allowed.
        unsafe {
            std::ptr::copy(
                source.buffer.contents().cast::<u8>().add(src_off),
                destination.buffer.contents().cast::<u8>().add(dst_off),
                len,
            )
        };
        Ok(())
    }
    fn record_compute(&self) -> Result<()> {
        self.finish()
    }
    fn record_copies(&self, _queue: &()) -> Result<()> {
        Ok(())
    }
    fn copies_wait(&self, _queue: &(), _event: &()) -> Result<()> {
        Ok(())
    }
    fn compute_wait(&self, _event: &()) -> Result<()> {
        Ok(())
    }
    fn event_done(&self, _event: &()) -> Result<bool> {
        Ok(true)
    }
    fn event_wait(&self, _event: &()) -> Result<()> {
        Ok(())
    }
    fn copies_sync(&self, _queue: &()) -> Result<()> {
        Ok(())
    }
}
