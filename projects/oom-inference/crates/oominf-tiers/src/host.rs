//! Host tier plumbing: a page-locked arena of record slots and an io_uring reader
//! that fills them from `experts.bin` with O_DIRECT (no page cache).

use std::fs::{File, OpenOptions};
use std::os::fd::AsRawFd;
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;

use std::sync::Arc;

use anyhow::{Context, Result, bail, ensure};
use io_uring::{IoUring, opcode, types};
use oominf_core::Transfer;

/// Anonymous, page-aligned host memory pinned once with the backend, so copies from
/// it to the device are asynchronous. Slots are `stride` bytes (a multiple of 4096,
/// as O_DIRECT requires).
pub struct PinnedArena<B: Transfer> {
    b: Arc<B>,
    ptr: *mut u8,
    bytes: usize,
    stride: usize,
    slots: usize,
}

impl<B: Transfer> PinnedArena<B> {
    pub fn new(b: Arc<B>, slots: usize, stride: usize) -> Result<Self> {
        ensure!(
            stride.is_multiple_of(4096),
            "slot stride {stride} is not 4096-aligned"
        );
        let bytes = slots * stride;
        ensure!(bytes > 0, "empty host arena");
        // SAFETY: anonymous private mapping; checked for MAP_FAILED.
        let ptr = unsafe {
            libc::mmap(
                std::ptr::null_mut(),
                bytes,
                libc::PROT_READ | libc::PROT_WRITE,
                libc::MAP_PRIVATE | libc::MAP_ANONYMOUS,
                -1,
                0,
            )
        };
        if ptr == libc::MAP_FAILED {
            bail!("mmap of {bytes} bytes for the host tier failed");
        }
        // SAFETY: ptr/bytes describe the mapping above, which lives until Drop
        // unpins it.
        if let Err(e) = unsafe { b.pin_host(ptr.cast(), bytes) } {
            // SAFETY: unmapping the mapping created above.
            unsafe { libc::munmap(ptr, bytes) };
            return Err(e.context(format!("pinning {bytes} bytes for the host tier")));
        }
        Ok(PinnedArena {
            b,
            ptr: ptr.cast(),
            bytes,
            stride,
            slots,
        })
    }

    pub fn slots(&self) -> usize {
        self.slots
    }

    pub fn slot_ptr(&self, slot: usize) -> *mut u8 {
        assert!(slot < self.slots);
        // SAFETY: in bounds of the mapping.
        unsafe { self.ptr.add(slot * self.stride) }
    }

    pub fn stride(&self) -> usize {
        self.stride
    }
}

impl<B: Transfer> Drop for PinnedArena<B> {
    fn drop(&mut self) {
        // SAFETY: pinned and mapped in `new`.
        unsafe {
            self.b.unpin_host(self.ptr);
            libc::munmap(self.ptr.cast(), self.bytes);
        }
    }
}

/// Batched O_DIRECT reads of whole records through io_uring. A batch is queued with
/// [`DirectReader::submit`] (returns at once) and completed with
/// [`DirectReader::drain`], so the caller can do other work while the disk reads.
pub struct DirectReader {
    file: File,
    ring: IoUring,
    jobs: Vec<ReadJob>,
    next: usize,
    inflight: usize,
}

// SAFETY: the destinations are slots of a pinned arena owned by the same tier, which
// moves between threads together with the reader.
unsafe impl Send for DirectReader {}

/// One record read: file offset, destination, and a caller tag.
pub struct ReadJob {
    pub offset: u64,
    pub dst: *mut u8,
    pub len: usize,
    pub tag: usize,
}

impl DirectReader {
    pub fn open(path: &Path) -> Result<Self> {
        let file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_DIRECT)
            .open(path)
            .with_context(|| format!("open O_DIRECT {}", path.display()))?;
        Ok(DirectReader {
            file,
            ring: IoUring::new(256)?,
            jobs: Vec::new(),
            next: 0,
            inflight: 0,
        })
    }

    /// True while a submitted batch has not been drained.
    pub fn busy(&self) -> bool {
        self.next < self.jobs.len() || self.inflight > 0
    }

    /// Queues `jobs` and submits as many as the ring holds, without waiting.
    pub fn submit(&mut self, jobs: Vec<ReadJob>) -> Result<()> {
        ensure!(!self.busy(), "previous read batch not drained");
        for j in &jobs {
            ensure!(
                j.offset.is_multiple_of(4096) && j.len.is_multiple_of(4096),
                "unaligned O_DIRECT read"
            );
        }
        self.jobs = jobs;
        self.next = 0;
        self.push()?;
        self.ring.submit()?;
        Ok(())
    }

    /// Waits for the batch, calling `done(tag)` as each read completes (so the caller
    /// can start its device copy while the rest read). On error the batch is
    /// abandoned after every submitted read has finished.
    pub fn drain(&mut self, mut done: impl FnMut(usize) -> Result<()>) -> Result<()> {
        let mut first_err = None;
        while self.busy() {
            self.ring.submit_and_wait(1)?;
            let cq: Vec<(u64, i32)> = self
                .ring
                .completion()
                .map(|c| (c.user_data(), c.result()))
                .collect();
            for (i, res) in cq {
                self.inflight -= 1;
                let j = &self.jobs[i as usize];
                if first_err.is_some() {
                    continue;
                }
                if res < 0 || res as usize != j.len {
                    first_err = Some(anyhow::anyhow!(
                        "O_DIRECT read of {} bytes at {} returned {res}",
                        j.len,
                        j.offset
                    ));
                    self.next = self.jobs.len();
                } else if let Err(e) = done(j.tag) {
                    first_err = Some(e);
                    self.next = self.jobs.len();
                }
            }
            if first_err.is_none() {
                self.push()?;
            }
        }
        match first_err {
            Some(e) => Err(e),
            None => Ok(()),
        }
    }

    /// Pushes queued jobs into free submission slots.
    fn push(&mut self) -> Result<()> {
        let fd = types::Fd(self.file.as_raw_fd());
        let depth = self.ring.params().sq_entries() as usize;
        while self.next < self.jobs.len() && self.inflight < depth {
            let j = &self.jobs[self.next];
            let sqe = opcode::Read::new(fd, j.dst, j.len as u32)
                .offset(j.offset)
                .build()
                .user_data(self.next as u64);
            // SAFETY: dst stays valid and untouched until this read completes.
            unsafe { self.ring.submission().push(&sqe) }
                .map_err(|_| anyhow::anyhow!("io_uring submission queue full"))?;
            self.next += 1;
            self.inflight += 1;
        }
        Ok(())
    }
}
