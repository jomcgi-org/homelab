//! Small host-to-device uploads through pinned staging buffers. An upload from
//! pageable memory makes the host wait for the compute stream to drain; one from
//! pinned memory only queues, so the host keeps queueing work while the device
//! runs (routing tables, PLE rows, masks).

use anyhow::Result;
use std::sync::Arc;

use cudarc::driver::{CudaContext, CudaEvent, CudaStream, result, sys};

/// Uploads up to this size go through the staging ring; larger ones (weights at
/// load) copy from pageable memory.
pub(crate) const STAGING_MAX: usize = 1 << 20;

/// Pinned buffers in flight at once; reusing one waits for its previous upload.
const SLOTS: usize = 64;

struct Slot {
    ptr: *mut u8,
    cap: usize,
    done: Option<CudaEvent>,
}

pub(crate) struct Staging {
    ctx: Arc<CudaContext>,
    slots: Vec<Slot>,
    next: usize,
}

// SAFETY: the pinned buffers are plain host memory owned by the ring, used only
// under the `Gpu`'s mutex.
unsafe impl Send for Staging {}

impl Staging {
    pub(crate) fn new(ctx: Arc<CudaContext>) -> Self {
        let slots = (0..SLOTS)
            .map(|_| Slot {
                ptr: std::ptr::null_mut(),
                cap: 0,
                done: None,
            })
            .collect();
        Self {
            ctx,
            slots,
            next: 0,
        }
    }

    /// Queues on `stream` a copy of `src` to device address `dst`, through the next
    /// slot.
    ///
    /// # Safety
    /// `dst` must be valid device memory of `src.len()` bytes, written in stream
    /// order like any other upload on `stream`.
    pub(crate) unsafe fn upload(
        &mut self,
        stream: &CudaStream,
        dst: u64,
        src: &[u8],
    ) -> Result<()> {
        let slot = &mut self.slots[self.next];
        self.next = (self.next + 1) % SLOTS;
        if let Some(ev) = slot.done.take() {
            ev.synchronize()?;
        }
        if slot.cap < src.len() {
            self.ctx.bind_to_thread()?;
            // SAFETY: the slot's previous upload completed above.
            unsafe {
                if !slot.ptr.is_null() {
                    result::free_host(slot.ptr.cast())?;
                }
                let cap = src.len().next_power_of_two().max(64 << 10);
                slot.ptr = result::malloc_host(cap, 0)?.cast();
                slot.cap = cap;
            }
        }
        // SAFETY: the slot holds at least `src.len()` bytes and no copy reads it.
        unsafe {
            std::ptr::copy_nonoverlapping(src.as_ptr(), slot.ptr, src.len());
            let pinned = std::slice::from_raw_parts(slot.ptr, src.len());
            result::memcpy_htod_async(dst, pinned, stream.cu_stream())?;
        }
        slot.done = Some(stream.record_event(Some(sys::CUevent_flags::CU_EVENT_DISABLE_TIMING))?);
        Ok(())
    }
}

impl Drop for Staging {
    fn drop(&mut self) {
        let _ = self.ctx.bind_to_thread();
        for slot in &mut self.slots {
            if let Some(ev) = slot.done.take() {
                let _ = ev.synchronize();
            }
            if !slot.ptr.is_null() {
                // SAFETY: allocated by malloc_host; its upload has completed.
                let _ = unsafe { result::free_host(slot.ptr.cast()) };
            }
        }
    }
}
