//! Host tier plumbing: a page-locked arena of record slots and a reader (on its
//! own threads) that fills them from `experts.bin` without the page cache.
//!
//! The reader prefers O_DIRECT reads through io_uring. Where that is unavailable
//! (io_uring blocked by a container's seccomp profile or an old kernel, or a
//! filesystem without O_DIRECT) it falls back to `pread` on a pool of threads,
//! with O_DIRECT when the filesystem allows it ([`ReadMode`]).

#[cfg(target_os = "linux")]
use std::collections::HashMap;
use std::collections::{HashSet, VecDeque};
use std::fs::{File, OpenOptions};
use std::os::fd::AsRawFd;
use std::os::unix::fs::FileExt;
#[cfg(target_os = "linux")]
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;
use std::sync::{Arc, Condvar, Mutex, mpsc};
use std::thread;
use std::time::Duration;

use anyhow::{Context, Result, bail, ensure};
#[cfg(target_os = "linux")]
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

    /// Pins up to `slots` slots, retrying at three quarters of the size while
    /// mapping or pinning fails, down to `min_slots`. Returns the arena and, when
    /// it is smaller than asked, why.
    pub fn new_at_most(
        b: Arc<B>,
        slots: usize,
        min_slots: usize,
        stride: usize,
    ) -> Result<(Self, Option<String>)> {
        let mut n = slots.max(1);
        let min = min_slots.clamp(1, n);
        loop {
            match Self::new(b.clone(), n, stride) {
                Ok(a) if n == slots => return Ok((a, None)),
                Ok(a) => {
                    return Ok((
                        a,
                        Some(format!(
                            "pinned {n} of {slots} requested slots ({:.1} of {:.1} GiB)",
                            (n * stride) as f64 / (1u64 << 30) as f64,
                            (slots * stride) as f64 / (1u64 << 30) as f64
                        )),
                    ));
                }
                Err(e) if n <= min => {
                    return Err(e.context(format!(
                        "could not pin even the minimum {min} slots of {stride} bytes"
                    )));
                }
                Err(_) => n = (n * 3 / 4).max(min),
            }
        }
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

/// How record reads reach the drive.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ReadMode {
    /// O_DIRECT reads through io_uring, submitted from one thread (the default).
    Uring,
    /// O_DIRECT `pread` on a pool of threads: io_uring is unavailable.
    Pread,
    /// Buffered `pread` on a pool of threads, dropping the read pages from the page
    /// cache afterwards: the filesystem does not support O_DIRECT.
    Buffered,
}

impl ReadMode {
    pub const ALL: [ReadMode; 3] = [ReadMode::Uring, ReadMode::Pread, ReadMode::Buffered];

    /// `auto` (`None`: the fastest that works), `uring`, `pread` or `buffered`.
    pub fn parse(s: &str) -> Result<Option<ReadMode>> {
        Ok(match s {
            "auto" => None,
            "uring" => Some(ReadMode::Uring),
            "pread" => Some(ReadMode::Pread),
            "buffered" => Some(ReadMode::Buffered),
            other => bail!("unknown read path {other:?} (auto, uring, pread, buffered)"),
        })
    }

    pub fn name(self) -> &'static str {
        match self {
            ReadMode::Uring => "uring",
            ReadMode::Pread => "pread",
            ReadMode::Buffered => "buffered",
        }
    }

    pub fn describe(self) -> &'static str {
        match self {
            ReadMode::Uring => "O_DIRECT through io_uring",
            #[cfg(target_os = "macos")]
            ReadMode::Pread => "F_NOCACHE pread on a thread pool",
            #[cfg(not(target_os = "macos"))]
            ReadMode::Pread => "O_DIRECT pread on a thread pool (io_uring unavailable)",
            #[cfg(target_os = "linux")]
            ReadMode::Buffered => {
                "buffered pread on a thread pool, pages dropped after each read (O_DIRECT unavailable)"
            }
            #[cfg(not(target_os = "linux"))]
            ReadMode::Buffered => "buffered pread on a thread pool",
        }
    }

    fn direct(self) -> bool {
        self != ReadMode::Buffered
    }
}

/// Opens `path` for reads in `mode`.
fn open_for(path: &Path, mode: ReadMode) -> Result<File> {
    #[cfg(not(target_os = "linux"))]
    ensure!(mode != ReadMode::Uring, "io_uring requires Linux");
    let mut o = OpenOptions::new();
    o.read(true);
    #[cfg(target_os = "linux")]
    if mode.direct() {
        o.custom_flags(libc::O_DIRECT);
    }
    let file = o
        .open(path)
        .with_context(|| format!("open {} for {}", path.display(), mode.name()))?;
    #[cfg(target_os = "macos")]
    if mode.direct() {
        // SAFETY: these commands take integer flags on a live descriptor.
        ensure!(
            unsafe { libc::fcntl(file.as_raw_fd(), libc::F_NOCACHE, 1) } == 0,
            "F_NOCACHE: {}",
            std::io::Error::last_os_error()
        );
        ensure!(
            unsafe { libc::fcntl(file.as_raw_fd(), libc::F_RDAHEAD, 0) } == 0,
            "F_RDAHEAD: {}",
            std::io::Error::last_os_error()
        );
    }
    Ok(file)
}

/// A page-aligned heap buffer (for test reads).
struct Aligned(*mut u8, std::alloc::Layout);

impl Aligned {
    fn new(len: usize) -> Aligned {
        let layout = std::alloc::Layout::from_size_align(len, 4096).expect("valid layout");
        // SAFETY: non-zero size.
        let p = unsafe { std::alloc::alloc_zeroed(layout) };
        assert!(!p.is_null(), "allocation failed");
        Aligned(p, layout)
    }
}

impl Drop for Aligned {
    fn drop(&mut self) {
        // SAFETY: allocated in `new` with this layout.
        unsafe { std::alloc::dealloc(self.0, self.1) };
    }
}

/// Reads the first 4096 bytes of `path` in `mode` (does the path work here?).
fn try_mode(path: &Path, mode: ReadMode) -> Result<()> {
    let file = open_for(path, mode)?;
    let buf = Aligned::new(4096);
    match mode {
        ReadMode::Uring => {
            #[cfg(not(target_os = "linux"))]
            bail!("io_uring requires Linux");
            #[cfg(target_os = "linux")]
            {
                let mut ring = IoUring::new(2).context("io_uring setup")?;
                let sqe = opcode::Read::new(types::Fd(file.as_raw_fd()), buf.0, 4096)
                    .offset(0)
                    .build()
                    .user_data(1);
                // SAFETY: the buffer outlives the read, which completes below.
                unsafe { ring.submission().push(&sqe) }
                    .map_err(|_| anyhow::anyhow!("ring full"))?;
                ring.submit_and_wait(1).context("io_uring submit")?;
                let res = ring
                    .completion()
                    .next()
                    .context("no io_uring completion")?
                    .result();
                ensure!(
                    res >= 0,
                    "io_uring read: {}",
                    std::io::Error::from_raw_os_error(-res)
                );
            }
        }
        ReadMode::Pread | ReadMode::Buffered => {
            // SAFETY: the buffer holds 4096 bytes.
            let dst = unsafe { std::slice::from_raw_parts_mut(buf.0, 4096) };
            let n = file
                .read_at(dst, 0)
                .with_context(|| format!("{} read", mode.name()))?;
            ensure!(n > 0, "{} read returned nothing", mode.name());
        }
    }
    Ok(())
}

/// The read path for `path`: `forced`, which must work, or the first of
/// [`ReadMode::ALL`] that does. Also returns why faster paths were skipped.
pub fn select_read_mode(path: &Path, forced: Option<ReadMode>) -> Result<(ReadMode, Vec<String>)> {
    if let Some(m) = forced {
        try_mode(path, m).with_context(|| format!("read path {} does not work here", m.name()))?;
        return Ok((m, Vec::new()));
    }
    let mut skipped = Vec::new();
    for m in ReadMode::ALL {
        match try_mode(path, m) {
            Ok(()) => return Ok((m, skipped)),
            Err(e) => skipped.push(format!("{}: {e:#}", m.name())),
        }
    }
    bail!(
        "no read path works for {}: {}",
        path.display(),
        skipped.join("; ")
    )
}

/// A fault a test injects into one read: wait `delay` before the read writes its
/// buffer, then report it failed when `fail` (the buffer is still written, as a
/// failing device may have done).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Fault {
    pub delay: Duration,
    pub fail: bool,
}

/// Decides the fault of each read by its file offset (fault injection in tests).
pub type FaultHook = Arc<dyn Fn(u64) -> Fault + Send + Sync>;

/// How a tier's readers read: the path, and faults to inject (tests only).
#[derive(Clone)]
pub struct IoConfig {
    pub mode: ReadMode,
    pub faults: Option<FaultHook>,
}

impl IoConfig {
    pub fn new(mode: ReadMode) -> Self {
        IoConfig { mode, faults: None }
    }
}

impl Default for IoConfig {
    fn default() -> Self {
        IoConfig::new(if cfg!(target_os = "linux") {
            ReadMode::Uring
        } else {
            ReadMode::Pread
        })
    }
}

type Report = (usize, std::result::Result<(), String>);

/// Where submitted reads go.
enum Sink {
    /// The io_uring worker's queue.
    #[cfg(target_os = "linux")]
    Uring(mpsc::Sender<Vec<ReadJob>>),
    /// The `pread` pool's shared queue.
    Pool(Arc<Pool>),
}

/// Batched reads of whole records on worker threads (see [`ReadMode`]). A batch is
/// queued with [`DirectReader::submit`] (returns at once) and completed with
/// [`DirectReader::drain`] or, read by read, [`DirectReader::poll`], so the caller
/// does other work while the disk reads. Submitting can block in the kernel once
/// the device's request queue is full (a record is many block requests), so the
/// workers submit and the caller never waits for anything but its data.
pub struct DirectReader {
    sink: Option<Sink>,
    done: mpsc::Receiver<Report>,
    workers: Vec<thread::JoinHandle<()>>,
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
// reader joins its workers before the arena can be dropped.
unsafe impl Send for ReadJob {}

impl DirectReader {
    /// Opens `path` with O_DIRECT reads through io_uring, up to `depth` in flight.
    pub fn open(path: &Path, depth: u32) -> Result<Self> {
        Self::open_with(path, depth, &IoConfig::default())
    }

    /// Opens `path` for reads as `io` says, with up to `depth` reads in flight (ring
    /// entries or pool threads). One multi-megabyte record is already many block
    /// requests, so a few reads keep a drive busy; a shallow reader leaves the
    /// device queue to a deeper one's urgent reads.
    pub fn open_with(path: &Path, depth: u32, io: &IoConfig) -> Result<Self> {
        let file = Arc::new(open_for(path, io.mode)?);
        let (done_tx, done_rx) = mpsc::channel();
        let faults = io.faults.clone();
        let (sink, workers) = match io.mode {
            ReadMode::Uring => {
                #[cfg(not(target_os = "linux"))]
                bail!("io_uring requires Linux");
                #[cfg(target_os = "linux")]
                {
                    let ring = IoUring::new(depth).context("io_uring setup")?;
                    let (jobs_tx, jobs_rx) = mpsc::channel();
                    let worker = thread::Builder::new()
                        .name("oominf-read".into())
                        .spawn(move || uring_worker(file, ring, jobs_rx, done_tx, faults))?;
                    (Sink::Uring(jobs_tx), vec![worker])
                }
            }
            ReadMode::Pread | ReadMode::Buffered => {
                let pool = Arc::new(Pool::default());
                let workers = (0..depth.max(1))
                    .map(|i| {
                        let (pool, file, done, faults) =
                            (pool.clone(), file.clone(), done_tx.clone(), faults.clone());
                        let drop_cache = io.mode == ReadMode::Buffered;
                        thread::Builder::new()
                            .name(format!("oominf-read-{i}"))
                            .spawn(move || pool_worker(&pool, &file, &done, faults, drop_cache))
                    })
                    .collect::<std::io::Result<Vec<_>>>()?;
                (Sink::Pool(pool), workers)
            }
        };
        Ok(DirectReader {
            sink: Some(sink),
            done: done_rx,
            workers,
            outstanding: 0,
            failed: None,
        })
    }

    /// True while a submitted batch has not been drained.
    pub fn busy(&self) -> bool {
        self.outstanding > 0
    }

    /// Queues `jobs` for the workers, without waiting (behind any still in flight;
    /// tags must be distinct among the reads outstanding).
    pub fn submit(&mut self, jobs: Vec<ReadJob>) -> Result<()> {
        for j in &jobs {
            ensure!(
                j.offset.is_multiple_of(4096) && j.len.is_multiple_of(4096),
                "unaligned O_DIRECT read"
            );
        }
        let n = jobs.len();
        match self.sink.as_ref().expect("workers running") {
            #[cfg(target_os = "linux")]
            Sink::Uring(tx) => tx
                .send(jobs)
                .map_err(|_| anyhow::anyhow!("read worker stopped"))?,
            Sink::Pool(pool) => pool.push(jobs),
        }
        self.outstanding += n;
        Ok(())
    }

    fn recv(&self) -> Result<Report> {
        self.done
            .recv()
            .map_err(|_| anyhow::anyhow!("read worker stopped"))
    }

    /// Waits for the batch, calling `done(tag)` as each read completes (so the caller
    /// can start its device copy while the rest read). On error the batch is
    /// abandoned after every submitted read has finished.
    pub fn drain(&mut self, mut done: impl FnMut(usize) -> Result<()>) -> Result<()> {
        let mut first_err = self.failed.take();
        while self.outstanding > 0 {
            let report = self.recv()?;
            self.complete(report, &mut first_err, &mut done);
        }
        match first_err {
            Some(e) => Err(e),
            None => Ok(()),
        }
    }

    /// Like [`DirectReader::drain`], but waits only until every read tagged in
    /// `need` has completed; the others stay in flight. `done` sees every read that
    /// completes meanwhile.
    pub fn wait_for(
        &mut self,
        need: &HashSet<usize>,
        mut done: impl FnMut(usize) -> Result<()>,
    ) -> Result<()> {
        let mut err = self.failed.take();
        let mut left = need.len();
        while left > 0 && self.outstanding > 0 {
            let report = self.recv()?;
            if need.contains(&report.0) {
                left -= 1;
            }
            self.complete(report, &mut err, &mut done);
        }
        match err {
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
        (tag, res): Report,
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
        // Closing the queue stops the workers once their reads are done, so no read
        // can still be writing into a destination after the reader is gone.
        match self.sink.take() {
            Some(Sink::Pool(pool)) => pool.close(),
            #[cfg(target_os = "linux")]
            Some(Sink::Uring(_)) => {}
            None => {}
        }
        for w in self.workers.drain(..) {
            let _ = w.join();
        }
    }
}

/// Reads `job` with `pread`, after the fault the hook gives it.
fn pread_job(file: &File, job: &ReadJob, fault: Fault, drop_cache: bool) -> Report {
    if !fault.delay.is_zero() {
        thread::sleep(fault.delay);
    }
    // SAFETY: the destination holds `len` bytes and is untouched by the tier until
    // this read is reported.
    let dst = unsafe { std::slice::from_raw_parts_mut(job.dst, job.len) };
    let mut at = 0;
    let mut res = Ok(());
    while at < job.len {
        match file.read_at(&mut dst[at..], job.offset + at as u64) {
            Ok(0) => {
                res = Err(format!("read of {} bytes hit end of file", job.len));
                break;
            }
            Ok(n) => at += n,
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => {}
            Err(e) => {
                res = Err(format!("read of {} bytes: {e}", job.len));
                break;
            }
        }
    }
    #[cfg(target_os = "linux")]
    if drop_cache {
        // SAFETY: plain advice on an open descriptor.
        unsafe {
            libc::posix_fadvise(
                file.as_raw_fd(),
                job.offset as libc::off_t,
                job.len as libc::off_t,
                libc::POSIX_FADV_DONTNEED,
            )
        };
    }
    #[cfg(not(target_os = "linux"))]
    let _ = drop_cache;
    if fault.fail && res.is_ok() {
        res = Err("injected read failure".into());
    }
    (job.tag, res)
}

/// The `pread` pool's queue.
#[derive(Default)]
struct Pool {
    queue: Mutex<(VecDeque<ReadJob>, bool)>,
    ready: Condvar,
}

impl Pool {
    fn push(&self, jobs: Vec<ReadJob>) {
        self.queue.lock().unwrap().0.extend(jobs);
        self.ready.notify_all();
    }

    fn close(&self) {
        self.queue.lock().unwrap().1 = true;
        self.ready.notify_all();
    }

    /// The next job, or `None` once closed and empty.
    fn pop(&self) -> Option<ReadJob> {
        let mut q = self.queue.lock().unwrap();
        loop {
            if let Some(j) = q.0.pop_front() {
                return Some(j);
            }
            if q.1 {
                return None;
            }
            q = self.ready.wait(q).unwrap();
        }
    }
}

fn pool_worker(
    pool: &Pool,
    file: &File,
    done: &mpsc::Sender<Report>,
    faults: Option<FaultHook>,
    drop_cache: bool,
) {
    while let Some(job) = pool.pop() {
        let fault = faults.as_ref().map(|f| f(job.offset)).unwrap_or_default();
        let _ = done.send(pread_job(file, &job, fault, drop_cache));
    }
}

/// Keeps up to the ring's depth of reads in flight and reports each by tag. Reads
/// with an injected fault run on their own thread instead, joined before the
/// worker exits.
#[cfg(target_os = "linux")]
fn uring_worker(
    file: Arc<File>,
    mut ring: IoUring,
    jobs: mpsc::Receiver<Vec<ReadJob>>,
    done: mpsc::Sender<Report>,
    faults: Option<FaultHook>,
) {
    let fd = types::Fd(file.as_raw_fd());
    let mut queue: VecDeque<ReadJob> = VecDeque::new();
    // In-flight reads by ring entry: (tag, len).
    let mut inflight: HashMap<u64, (usize, usize)> = HashMap::new();
    let mut faulted: Vec<thread::JoinHandle<()>> = Vec::new();
    let depth = ring.params().sq_entries() as usize;
    let mut next_id = 0u64;
    loop {
        if queue.is_empty() && inflight.is_empty() {
            match jobs.recv() {
                Ok(batch) => queue.extend(batch),
                Err(_) => break,
            }
        }
        while let Ok(batch) = jobs.try_recv() {
            queue.extend(batch);
        }
        if let Some(hook) = &faults {
            let mut rest = VecDeque::with_capacity(queue.len());
            for j in queue.drain(..) {
                let fault = hook(j.offset);
                if fault == Fault::default() {
                    rest.push_back(j);
                    continue;
                }
                let (file, done) = (file.clone(), done.clone());
                faulted.push(thread::spawn(move || {
                    let _ = done.send(pread_job(&file, &j, fault, false));
                }));
            }
            queue = rest;
            if queue.is_empty() && inflight.is_empty() {
                continue;
            }
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
    for t in faulted {
        let _ = t.join();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A file of `n` 4096-byte blocks, block `i` filled with byte `i`.
    fn blocks(n: usize) -> tempfile::NamedTempFile {
        let f = tempfile::NamedTempFile::new().unwrap();
        let data: Vec<u8> = (0..n).flat_map(|i| vec![i as u8; 4096]).collect();
        std::fs::write(f.path(), data).unwrap();
        f
    }

    /// Reads every block of `f` through a reader in `mode`; checks the bytes.
    fn read_all(f: &Path, mode: ReadMode, n: usize, faults: Option<FaultHook>) -> Result<()> {
        let mut r = DirectReader::open_with(f, 4, &IoConfig { mode, faults })?;
        let buf = Aligned::new(n * 4096);
        let jobs = (0..n)
            .map(|i| ReadJob {
                offset: (i * 4096) as u64,
                // SAFETY: within `buf`.
                dst: unsafe { buf.0.add(i * 4096) },
                len: 4096,
                tag: i,
            })
            .collect();
        r.submit(jobs)?;
        let mut seen = HashSet::new();
        r.drain(|t| {
            seen.insert(t);
            Ok(())
        })?;
        assert_eq!(seen.len(), n);
        // SAFETY: every read landed.
        let got = unsafe { std::slice::from_raw_parts(buf.0, n * 4096) };
        for (i, b) in got.chunks(4096).enumerate() {
            assert!(b.iter().all(|&x| x == i as u8), "block {i}");
        }
        Ok(())
    }

    #[test]
    fn every_read_path_reads_the_same_bytes() {
        let f = blocks(32);
        let (auto, _) = select_read_mode(f.path(), None).unwrap();
        for mode in ReadMode::ALL {
            // A path this machine lacks (e.g. io_uring under seccomp) is skipped.
            if try_mode(f.path(), mode).is_err() {
                assert_ne!(mode, auto);
                continue;
            }
            read_all(f.path(), mode, 32, None).unwrap();
        }
    }

    #[test]
    fn delayed_and_failed_reads_are_reported_after_they_finish() {
        let f = blocks(8);
        for mode in ReadMode::ALL {
            if try_mode(f.path(), mode).is_err() {
                continue;
            }
            let slow: FaultHook = Arc::new(|off| Fault {
                delay: Duration::from_millis(if off == 0 { 50 } else { 0 }),
                fail: false,
            });
            read_all(f.path(), mode, 8, Some(slow)).unwrap();
            let bad: FaultHook = Arc::new(|off| Fault {
                delay: Duration::from_millis(20),
                fail: off == 3 * 4096,
            });
            let e = read_all(f.path(), mode, 8, Some(bad)).unwrap_err();
            assert!(format!("{e:#}").contains("injected"), "{mode:?}: {e:#}");
        }
    }

    #[test]
    fn forcing_a_path_that_does_not_work_fails_clearly() {
        let missing = Path::new("/nonexistent/experts.bin");
        let e = select_read_mode(missing, Some(ReadMode::Pread)).unwrap_err();
        assert!(format!("{e:#}").contains("read path pread"), "{e:#}");
        assert!(select_read_mode(missing, None).is_err());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn macos_selects_uncached_pread_and_rejects_uring() {
        let f = blocks(4);
        let (mode, skipped) = select_read_mode(f.path(), None).unwrap();
        assert_eq!(mode, ReadMode::Pread);
        assert_eq!(IoConfig::default().mode, ReadMode::Pread);
        assert!(skipped[0].contains("io_uring requires Linux"));
        assert!(select_read_mode(f.path(), Some(ReadMode::Uring)).is_err());
        read_all(f.path(), mode, 4, None).unwrap();
    }
}
