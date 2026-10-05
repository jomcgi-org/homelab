//! Host tier plumbing: a page-locked arena of record slots and an io_uring reader
//! (on its own thread) that fills them from `experts.bin` with O_DIRECT (no page
//! cache).

use std::collections::{HashMap, VecDeque};
use std::fs::{File, OpenOptions};
use std::os::fd::AsRawFd;
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;
use std::sync::{Arc, mpsc};
use std::thread;

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

/// Batched O_DIRECT reads of whole records through io_uring on a worker thread. A
/// batch is queued with [`DirectReader::submit`] (returns at once) and completed
/// with [`DirectReader::drain`] or, read by read, [`DirectReader::poll`], so the
/// caller does other work while the disk reads. Submitting can block in the kernel
/// once the device's request queue is full (a record is many block requests), so the
/// worker submits and the caller never waits for anything but its data.
pub struct DirectReader {
    jobs: Option<mpsc::Sender<Vec<ReadJob>>>,
    done: mpsc::Receiver<(usize, std::result::Result<(), String>)>,
    worker: Option<thread::JoinHandle<()>>,
    /// Reads submitted and not yet reported back.
    outstanding: usize,
    /// An error from [`DirectReader::poll`], returned by the next `drain`.
    failed: Option<anyhow::Error>,
}

/// One record read: file offset, destination, and a caller tag.
pub struct ReadJob {
    pub offset: u64,
    pub dst: *mut u8,
    pub len: usize,
    pub tag: usize,
}

// SAFETY: a job's destination is a slot of a pinned arena owned by the same tier as
// the reader; the tier does not touch it until the read is reported done, and the
// reader joins its worker before the arena can be dropped.
unsafe impl Send for ReadJob {}

impl DirectReader {
    /// Opens `path` with a worker that keeps up to `depth` reads in flight. One
    /// multi-megabyte record is already many block requests, so a few reads keep a
    /// drive busy; a shallow reader leaves the device queue to a deeper one's urgent
    /// reads.
    pub fn open(path: &Path, depth: u32) -> Result<Self> {
        let file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_DIRECT)
            .open(path)
            .with_context(|| format!("open O_DIRECT {}", path.display()))?;
        let ring = IoUring::new(depth)?;
        let (jobs_tx, jobs_rx) = mpsc::channel();
        let (done_tx, done_rx) = mpsc::channel();
        let worker = thread::Builder::new()
            .name("oominf-read".into())
            .spawn(move || read_worker(file, ring, jobs_rx, done_tx))?;
        Ok(DirectReader {
            jobs: Some(jobs_tx),
            done: done_rx,
            worker: Some(worker),
            outstanding: 0,
            failed: None,
        })
    }

    /// True while a submitted batch has not been drained.
    pub fn busy(&self) -> bool {
        self.outstanding > 0
    }

    /// Queues `jobs` for the worker, without waiting.
    pub fn submit(&mut self, jobs: Vec<ReadJob>) -> Result<()> {
        ensure!(!self.busy(), "previous read batch not drained");
        for j in &jobs {
            ensure!(
                j.offset.is_multiple_of(4096) && j.len.is_multiple_of(4096),
                "unaligned O_DIRECT read"
            );
        }
        self.outstanding = jobs.len();
        self.jobs
            .as_ref()
            .expect("worker running")
            .send(jobs)
            .map_err(|_| anyhow::anyhow!("read worker stopped"))
    }

    /// Waits for the batch, calling `done(tag)` as each read completes (so the caller
    /// can start its device copy while the rest read). On error the batch is
    /// abandoned after every submitted read has finished.
    pub fn drain(&mut self, mut done: impl FnMut(usize) -> Result<()>) -> Result<()> {
        let mut first_err = self.failed.take();
        while self.outstanding > 0 {
            let report = self
                .done
                .recv()
                .map_err(|_| anyhow::anyhow!("read worker stopped"))?;
            self.complete(report, &mut first_err, &mut done);
        }
        match first_err {
            Some(e) => Err(e),
            None => Ok(()),
        }
    }

    /// Like [`DirectReader::drain`] for the reads that have completed so far,
    /// without waiting for the rest. An error abandons the batch as in `drain`,
    /// which then returns it.
    pub fn poll(&mut self, mut done: impl FnMut(usize) -> Result<()>) -> Result<()> {
        let mut failed = self.failed.take();
        while self.outstanding > 0
            && let Ok(report) = self.done.try_recv()
        {
            self.complete(report, &mut failed, &mut done);
        }
        self.failed = failed;
        Ok(())
    }

    fn complete(
        &mut self,
        (tag, res): (usize, std::result::Result<(), String>),
        err: &mut Option<anyhow::Error>,
        done: &mut impl FnMut(usize) -> Result<()>,
    ) {
        self.outstanding -= 1;
        if err.is_some() {
            return;
        }
        match res {
            Err(e) => *err = Some(anyhow::anyhow!(e)),
            Ok(()) => {
                if let Err(e) = done(tag) {
                    *err = Some(e);
                }
            }
        }
    }
}

impl Drop for DirectReader {
    fn drop(&mut self) {
        // Closing the channel stops the worker once its reads are done, so no read
        // can still be writing into a destination after the reader is gone.
        self.jobs.take();
        if let Some(w) = self.worker.take() {
            let _ = w.join();
        }
    }
}

/// Keeps up to the ring's depth of reads in flight and reports each by tag.
fn read_worker(
    file: File,
    mut ring: IoUring,
    jobs: mpsc::Receiver<Vec<ReadJob>>,
    done: mpsc::Sender<(usize, std::result::Result<(), String>)>,
) {
    let fd = types::Fd(file.as_raw_fd());
    let mut queue: VecDeque<ReadJob> = VecDeque::new();
    // In-flight reads by ring entry: (tag, len).
    let mut inflight: HashMap<u64, (usize, usize)> = HashMap::new();
    let depth = ring.params().sq_entries() as usize;
    let mut next_id = 0u64;
    loop {
        if queue.is_empty() && inflight.is_empty() {
            match jobs.recv() {
                Ok(batch) => queue.extend(batch),
                Err(_) => return,
            }
        }
        while let Ok(batch) = jobs.try_recv() {
            queue.extend(batch);
        }
        while inflight.len() < depth
            && let Some(j) = queue.pop_front()
        {
            let sqe = opcode::Read::new(fd, j.dst, j.len as u32)
                .offset(j.offset)
                .build()
                .user_data(next_id);
            // SAFETY: dst stays valid and untouched until this read completes.
            if unsafe { ring.submission().push(&sqe) }.is_err() {
                queue.push_front(j);
                break;
            }
            inflight.insert(next_id, (j.tag, j.len));
            next_id += 1;
        }
        if let Err(e) = ring.submit_and_wait(1) {
            // Fail every read still pending; their buffers are not written.
            for (_, (tag, _)) in inflight.drain() {
                let _ = done.send((tag, Err(format!("io_uring submit: {e}"))));
            }
            for j in queue.drain(..) {
                let _ = done.send((j.tag, Err(format!("io_uring submit: {e}"))));
            }
            continue;
        }
        let cq: Vec<(u64, i32)> = ring
            .completion()
            .map(|c| (c.user_data(), c.result()))
            .collect();
        for (id, res) in cq {
            let Some((tag, len)) = inflight.remove(&id) else {
                continue;
            };
            let report = if res < 0 || res as usize != len {
                Err(format!("O_DIRECT read of {len} bytes returned {res}"))
            } else {
                Ok(())
            };
            let _ = done.send((tag, report));
        }
    }
}
