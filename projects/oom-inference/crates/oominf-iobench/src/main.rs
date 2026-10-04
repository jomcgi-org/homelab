//! Weight-layout I/O microbench for oom-inference.
//!
//! Compares loading routed experts from NVMe with two on-disk layouts that hold
//! the same bytes, using Qwen 3.8 Flash record geometry (see `docs/FORMAT.md`):
//!
//! * `expert`: expert-major records, one 4096-aligned record of 2,768,896 bytes
//!   per (layer, expert). One read per expert.
//! * `tensor`: tensor-major, each part type stored as one contiguous `[512, ..]`
//!   array per layer. Six reads per expert (the six weight and scale parts; the
//!   per-expert F32 scalars are assumed resident, which favours this layout).
//!
//! All reads are O_DIRECT into 4096-aligned buffers, one buffer slot per expert.
//!
//! # Running
//!
//! ```text
//! cargo build --release -p oominf-iobench
//! nice -n 19 taskset -c 8-15 target/release/oominf-iobench all \
//!     --dir /disks/nvme-02/src/oominf-data/iobench \
//!     --trace /disks/nvme-02/src/oominf-data/iobench/trace-routedump1.u16
//! ```
//!
//! `all` writes the two synthetic layout files (`--layers` layers each), runs the
//! decode-miss matrix and the layer-stream test, prints markdown tables and
//! deletes the files unless `--keep` is given. `prepare`, `decode` and `stream`
//! run the pieces individually; `ab` (needs the files) runs an interleaved A/B
//! of the latency to load k experts. Results: `docs/iobench-results.md`.
//!
//! A trace file is raw little-endian `u16` of shape `[steps, 48, 10]`: the top-10
//! routed experts of every MoE layer for each decode step.

use std::alloc::{self, Layout as AllocLayout};
use std::collections::VecDeque;
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::os::fd::AsRawFd;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, mpsc};
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{Context, Result, bail, ensure};
use clap::{Args, Parser, Subcommand, ValueEnum};
use io_uring::{IoUring, opcode, types};
use rand::rngs::StdRng;
use rand::seq::SliceRandom;
use rand::{Rng, SeedableRng};
use rand_distr::{Distribution, Zipf};

const ALIGN: u64 = 4096;
const NUM_EXPERTS: usize = 512;
const MODEL_LAYERS: usize = 48;
const TOP_K: usize = 10;
/// gate.weight, gate.weight_scale, up.weight, up.weight_scale, down.weight, down.weight_scale.
const PARTS: [u64; 6] = [819_200, 102_400, 819_200, 102_400, 819_200, 102_400];
/// Expert-major record stride: 256 B scalars + parts, rounded up to `ALIGN`.
const STRIDE: u64 = 2_768_896;
const MAX_INFLIGHT: usize = 16;

#[derive(Clone, Copy, Debug, PartialEq, Eq, ValueEnum)]
enum LayoutKind {
    Expert,
    Tensor,
}

impl LayoutKind {
    fn file_name(self) -> &'static str {
        match self {
            LayoutKind::Expert => "expert-major.bin",
            LayoutKind::Tensor => "tensor-major.bin",
        }
    }

    fn layer_bytes(self) -> u64 {
        match self {
            LayoutKind::Expert => NUM_EXPERTS as u64 * STRIDE,
            LayoutKind::Tensor => {
                tm_scalar_block() + PARTS.iter().sum::<u64>() * NUM_EXPERTS as u64
            }
        }
    }

    /// Reads that bring one expert into a slot buffer: `(file offset, len, offset in slot)`.
    fn expert_reads(self, layer: usize, expert: usize, out: &mut Vec<Read>) {
        let base = layer as u64 * self.layer_bytes();
        match self {
            LayoutKind::Expert => out.push(Read {
                off: base + expert as u64 * STRIDE,
                len: STRIDE,
                dst: 0,
            }),
            LayoutKind::Tensor => {
                let mut array_off = base + tm_scalar_block();
                let mut dst = 0;
                for &part in &PARTS {
                    out.push(Read {
                        off: array_off + expert as u64 * part,
                        len: part,
                        dst,
                    });
                    array_off += part * NUM_EXPERTS as u64;
                    dst += part;
                }
            }
        }
    }
}

/// Six `[512] f32` scalar arrays, each padded to `ALIGN`.
fn tm_scalar_block() -> u64 {
    6 * (NUM_EXPERTS as u64 * 4).next_multiple_of(ALIGN)
}

#[derive(Clone, Copy, Debug)]
struct Read {
    off: u64,
    len: u64,
    dst: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, ValueEnum)]
enum Engine {
    Uring,
    Pread,
}

#[derive(Parser)]
#[command(about = "Expert-major vs tensor-major weight layout I/O microbench")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Write both synthetic layout files.
    Prepare(PrepareArgs),
    /// Replay decode-time expert misses.
    Decode(DecodeArgs),
    /// Stream whole layers sequentially.
    Stream(StreamArgs),
    /// Interleaved A/B: latency to load k experts, alternating layouts per sample.
    Ab(AbArgs),
    /// Prepare, run the full matrix, print tables, clean up.
    All(AllArgs),
}

#[derive(Args, Clone)]
struct PrepareArgs {
    #[arg(long)]
    dir: PathBuf,
    /// Layers per layout file.
    #[arg(long, default_value_t = 3)]
    layers: usize,
}

#[derive(Args, Clone)]
struct DecodeArgs {
    #[arg(long)]
    dir: PathBuf,
    #[arg(long, value_enum)]
    layout: LayoutKind,
    /// Trace file, or `synthetic`.
    #[arg(long, default_value = "synthetic")]
    trace: String,
    /// Timed decode steps.
    #[arg(long, default_value_t = 48)]
    steps: usize,
    /// Untimed steps that warm the cache model first.
    #[arg(long, default_value_t = 256)]
    warmup: usize,
    /// Per-layer LRU capacity in experts (0: every routed expert misses).
    #[arg(long, default_value_t = 0)]
    cache: usize,
    /// Layers whose loads may be in flight at once.
    #[arg(long, default_value_t = 1)]
    inflight: usize,
    #[arg(long, value_enum, default_value_t = Engine::Uring)]
    engine: Engine,
    /// pread worker threads.
    #[arg(long, default_value_t = 10)]
    threads: usize,
    #[arg(long, default_value_t = 3)]
    runs: usize,
}

#[derive(Args, Clone)]
struct StreamArgs {
    #[arg(long)]
    dir: PathBuf,
    #[arg(long, value_enum)]
    layout: LayoutKind,
    #[arg(long, default_value_t = 4)]
    chunk_mib: u64,
    #[arg(long, default_value_t = 16)]
    qd: usize,
    #[arg(long, default_value_t = 3)]
    runs: usize,
}

#[derive(Args, Clone)]
struct AbArgs {
    #[arg(long)]
    dir: PathBuf,
    /// Samples per layout per k.
    #[arg(long, default_value_t = 500)]
    samples: usize,
    /// Experts loaded per sample (comma separated).
    #[arg(long, default_value = "1,2,4,10", value_delimiter = ',')]
    k: Vec<usize>,
}

#[derive(Args, Clone)]
struct AllArgs {
    #[arg(long)]
    dir: PathBuf,
    #[arg(long, default_value_t = 3)]
    layers: usize,
    /// Recorded routing trace (optional; synthetic always runs).
    #[arg(long)]
    trace: Option<PathBuf>,
    #[arg(long, default_value_t = 48)]
    steps: usize,
    #[arg(long, default_value_t = 3)]
    runs: usize,
    /// Keep the layout files afterwards.
    #[arg(long)]
    keep: bool,
}

fn main() -> Result<()> {
    match Cli::parse().cmd {
        Cmd::Prepare(a) => prepare(&a.dir, a.layers),
        Cmd::Decode(a) => {
            let layers = file_layers(&a.dir, a.layout)?;
            let trace = load_trace(&a.trace)?;
            print_decode_header();
            let row = decode(&a, layers, &trace)?;
            println!("{}", row.markdown(&a));
            Ok(())
        }
        Cmd::Stream(a) => {
            let layers = file_layers(&a.dir, a.layout)?;
            println!("{}", stream(&a, layers)?);
            Ok(())
        }
        Cmd::Ab(a) => ab(&a),
        Cmd::All(a) => all(&a),
    }
}

// ---------------------------------------------------------------- data files

fn prepare(dir: &Path, layers: usize) -> Result<()> {
    fs::create_dir_all(dir)?;
    for layout in [LayoutKind::Expert, LayoutKind::Tensor] {
        let path = dir.join(layout.file_name());
        let size = layers as u64 * layout.layer_bytes();
        if fs::metadata(&path)
            .map(|m| m.len() == size)
            .unwrap_or(false)
        {
            eprintln!("reusing {}", path.display());
            continue;
        }
        eprintln!("writing {} ({:.2} GB)", path.display(), size as f64 / 1e9);
        let mut f = File::create(&path)?;
        let mut buf = vec![0u8; 64 << 20];
        let mut state = 0x9e37_79b9_7f4a_7c15u64;
        let mut left = size;
        while left > 0 {
            for word in buf.as_chunks_mut::<8>().0 {
                state ^= state << 13;
                state ^= state >> 7;
                state ^= state << 17;
                *word = state.to_le_bytes();
            }
            let n = left.min(buf.len() as u64) as usize;
            f.write_all(&buf[..n])?;
            left -= n as u64;
        }
        f.sync_all()?;
        // SAFETY: valid fd; advisory only.
        unsafe { libc::posix_fadvise(f.as_raw_fd(), 0, 0, libc::POSIX_FADV_DONTNEED) };
    }
    Ok(())
}

fn file_layers(dir: &Path, layout: LayoutKind) -> Result<usize> {
    let path = dir.join(layout.file_name());
    let len = fs::metadata(&path)
        .with_context(|| format!("{} (run prepare)", path.display()))?
        .len();
    ensure!(
        len % layout.layer_bytes() == 0,
        "{} has a partial layer",
        path.display()
    );
    Ok((len / layout.layer_bytes()) as usize)
}

fn open_direct(dir: &Path, layout: LayoutKind) -> Result<File> {
    let path = dir.join(layout.file_name());
    OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECT)
        .open(&path)
        .with_context(|| format!("open O_DIRECT {}", path.display()))
}

// ---------------------------------------------------------------- traces

/// `[step][layer][k]` routed experts.
struct Trace {
    name: String,
    steps: Vec<[[u16; TOP_K]; MODEL_LAYERS]>,
}

fn load_trace(spec: &str) -> Result<Trace> {
    if spec == "synthetic" {
        return Ok(synthetic_trace(4096));
    }
    let bytes = fs::read(spec).with_context(|| format!("read trace {spec}"))?;
    let step_bytes = MODEL_LAYERS * TOP_K * 2;
    ensure!(
        bytes.len() % step_bytes == 0,
        "trace size is not a multiple of [48, 10] u16"
    );
    let steps = bytes
        .chunks_exact(step_bytes)
        .map(|s| {
            let mut step = [[0u16; TOP_K]; MODEL_LAYERS];
            for (i, v) in s.as_chunks::<2>().0.iter().enumerate() {
                step[i / TOP_K][i % TOP_K] = u16::from_le_bytes(*v);
            }
            step
        })
        .collect();
    let name = Path::new(spec)
        .file_stem()
        .map_or(spec.into(), |s| s.to_string_lossy().into_owned());
    Ok(Trace { name, steps })
}

/// Zipf(s = 1.0) over each layer's own random expert ranking, keeping 2-3 of the
/// previous step's experts (the recorded trace averages 2.5 of 10).
fn synthetic_trace(steps: usize) -> Trace {
    let mut rng = StdRng::seed_from_u64(42);
    let zipf = Zipf::new(NUM_EXPERTS as f64, 1.0).expect("valid zipf");
    let rankings: Vec<Vec<u16>> = (0..MODEL_LAYERS)
        .map(|_| {
            let mut r: Vec<u16> = (0..NUM_EXPERTS as u16).collect();
            r.shuffle(&mut rng);
            r
        })
        .collect();
    let mut out = Vec::with_capacity(steps);
    let mut prev = [[0u16; TOP_K]; MODEL_LAYERS];
    for s in 0..steps {
        let mut step = [[0u16; TOP_K]; MODEL_LAYERS];
        for l in 0..MODEL_LAYERS {
            let mut chosen: Vec<u16> = Vec::with_capacity(TOP_K);
            if s > 0 {
                let keep = rng.random_range(2..=3);
                let mut p = prev[l].to_vec();
                p.shuffle(&mut rng);
                chosen.extend_from_slice(&p[..keep]);
            }
            while chosen.len() < TOP_K {
                let rank = zipf.sample(&mut rng) as usize - 1;
                let e = rankings[l][rank];
                if !chosen.contains(&e) {
                    chosen.push(e);
                }
            }
            step[l].copy_from_slice(&chosen);
        }
        prev = step;
        out.push(step);
    }
    Trace {
        name: "synthetic-zipf1.0".into(),
        steps: out,
    }
}

// ---------------------------------------------------------------- cache model

/// Per-layer LRU over expert ids; `capacity == 0` means every access misses.
struct Lru {
    capacity: usize,
    stamp: Vec<Vec<u64>>,
    resident: Vec<usize>,
    clock: u64,
}

impl Lru {
    fn new(capacity: usize) -> Self {
        Self {
            capacity,
            stamp: vec![vec![0; NUM_EXPERTS]; MODEL_LAYERS],
            resident: vec![0; MODEL_LAYERS],
            clock: 0,
        }
    }

    /// Experts of `routed` that miss; updates recency and residency.
    fn access(&mut self, layer: usize, routed: &[u16]) -> Vec<u16> {
        self.clock += 1;
        if self.capacity == 0 {
            return routed.to_vec();
        }
        let stamps = &mut self.stamp[layer];
        let mut misses = Vec::new();
        for &e in routed {
            if stamps[e as usize] == 0 {
                misses.push(e);
            }
        }
        // Mark hits first so eviction never drops an expert this step needs.
        for &e in routed {
            if stamps[e as usize] != 0 {
                stamps[e as usize] = self.clock;
            }
        }
        for &e in &misses {
            if self.resident[layer] == self.capacity {
                let victim = stamps
                    .iter()
                    .enumerate()
                    .filter(|(i, s)| **s != 0 && !routed.contains(&(*i as u16)))
                    .min_by_key(|(_, s)| **s)
                    .map(|(i, _)| i)
                    .expect("capacity > 0 implies a resident victim");
                stamps[victim] = 0;
                self.resident[layer] -= 1;
            }
            stamps[e as usize] = self.clock;
            self.resident[layer] += 1;
        }
        misses
    }
}

/// One layer's loads for one step.
struct Job {
    disk_layer: usize,
    experts: Vec<u16>,
}

fn build_jobs(trace: &Trace, a: &DecodeArgs, disk_layers: usize) -> Result<Vec<Job>> {
    let warmup = if a.cache == 0 { 0 } else { a.warmup };
    ensure!(
        trace.steps.len() >= warmup + a.steps,
        "trace {} has {} steps, need {}",
        trace.name,
        trace.steps.len(),
        warmup + a.steps
    );
    let mut lru = Lru::new(a.cache);
    for step in &trace.steps[..warmup] {
        for (l, routed) in step.iter().enumerate() {
            lru.access(l, routed);
        }
    }
    let mut jobs = Vec::with_capacity(a.steps * MODEL_LAYERS);
    for step in &trace.steps[warmup..warmup + a.steps] {
        for (l, routed) in step.iter().enumerate() {
            jobs.push(Job {
                disk_layer: l % disk_layers,
                experts: lru.access(l, routed),
            });
        }
    }
    Ok(jobs)
}

// ---------------------------------------------------------------- buffers

struct Slots {
    ptr: *mut u8,
    layout: AllocLayout,
}

impl Slots {
    fn new(count: usize) -> Self {
        let layout = AllocLayout::from_size_align(count * STRIDE as usize, ALIGN as usize)
            .expect("valid slot layout");
        // SAFETY: non-zero size, valid alignment.
        let ptr = unsafe { alloc::alloc_zeroed(layout) };
        assert!(!ptr.is_null(), "slot allocation failed");
        // Fault every page in before timing.
        // SAFETY: ptr is valid for layout.size() bytes.
        unsafe { std::ptr::write_bytes(ptr, 1, layout.size()) };
        Self { ptr, layout }
    }

    fn at(&self, slot: usize, dst: u64) -> *mut u8 {
        // SAFETY: callers keep slot < count and dst + len <= STRIDE.
        unsafe { self.ptr.add(slot * STRIDE as usize + dst as usize) }
    }
}

impl Drop for Slots {
    fn drop(&mut self) {
        // SAFETY: allocated with this layout in `new`.
        unsafe { alloc::dealloc(self.ptr, self.layout) };
    }
}

// ---------------------------------------------------------------- decode

struct RunStats {
    wall: Duration,
    bytes: u64,
    reads: u64,
    /// Latency of each non-empty layer job.
    lat: Vec<Duration>,
}

struct DecodeRow {
    trace: String,
    misses_per_layer: f64,
    reads_per_layer: f64,
    p50_us: f64,
    p95_us: f64,
    p99_us: f64,
    ms_per_step: f64,
    gbps: f64,
}

impl DecodeRow {
    fn markdown(&self, a: &DecodeArgs) -> String {
        let engine = match a.engine {
            Engine::Uring => format!("uring x{}", a.inflight),
            Engine::Pread => format!("pread t{}", a.threads),
        };
        format!(
            "| {} | {} | {} | {} | {:.2} | {:.1} | {:.0} | {:.0} | {:.0} | {:.1} | {:.2} |",
            self.trace,
            match a.layout {
                LayoutKind::Expert => "expert",
                LayoutKind::Tensor => "tensor",
            },
            if a.cache == 0 {
                "none".to_string()
            } else {
                a.cache.to_string()
            },
            engine,
            self.misses_per_layer,
            self.reads_per_layer,
            self.p50_us,
            self.p95_us,
            self.p99_us,
            self.ms_per_step,
            self.gbps
        )
    }
}

fn print_decode_header() {
    println!(
        "| trace | layout | LRU/layer | engine | miss/layer | reads/layer | p50 us | p95 us | p99 us | ms/step | GB/s |"
    );
    println!("|---|---|---|---|---|---|---|---|---|---|---|");
}

fn decode(a: &DecodeArgs, disk_layers: usize, trace: &Trace) -> Result<DecodeRow> {
    ensure!(
        (1..=MAX_INFLIGHT).contains(&a.inflight),
        "inflight must be 1..={MAX_INFLIGHT}"
    );
    let jobs = build_jobs(trace, a, disk_layers)?;
    let file = open_direct(&a.dir, a.layout)?;
    let mut runs = Vec::with_capacity(a.runs);
    for _ in 0..a.runs {
        runs.push(match a.engine {
            Engine::Uring => decode_uring(&file, a.layout, &jobs, a.inflight)?,
            Engine::Pread => decode_pread(&file, a.layout, &jobs, a.threads)?,
        });
    }
    let misses: usize = jobs.iter().map(|j| j.experts.len()).sum();
    let pct = |r: &RunStats, q: f64| -> f64 {
        let mut l = r.lat.clone();
        if l.is_empty() {
            return 0.0;
        }
        l.sort_unstable();
        l[((l.len() - 1) as f64 * q).round() as usize].as_secs_f64() * 1e6
    };
    Ok(DecodeRow {
        trace: trace.name.clone(),
        misses_per_layer: misses as f64 / jobs.len() as f64,
        reads_per_layer: runs[0].reads as f64 / jobs.len() as f64,
        p50_us: median(runs.iter().map(|r| pct(r, 0.50))),
        p95_us: median(runs.iter().map(|r| pct(r, 0.95))),
        p99_us: median(runs.iter().map(|r| pct(r, 0.99))),
        ms_per_step: median(
            runs.iter()
                .map(|r| r.wall.as_secs_f64() * 1e3 / a.steps as f64),
        ),
        gbps: median(
            runs.iter()
                .map(|r| r.bytes as f64 / r.wall.as_secs_f64() / 1e9),
        ),
    })
}

fn median(it: impl Iterator<Item = f64>) -> f64 {
    let mut v: Vec<f64> = it.collect();
    v.sort_by(f64::total_cmp);
    v[v.len() / 2]
}

/// Keeps up to `inflight` layer jobs submitted; a job's latency runs from its
/// submission to its last completion.
fn decode_uring(
    file: &File,
    layout: LayoutKind,
    jobs: &[Job],
    inflight: usize,
) -> Result<RunStats> {
    let mut ring = IoUring::new(1024)?;
    let slots = Slots::new(inflight * TOP_K);
    let fd = types::Fd(file.as_raw_fd());
    // Per in-flight group g: remaining reads, submit time.
    let mut remaining = vec![0usize; inflight];
    let mut started = vec![Instant::now(); inflight];
    let mut free: Vec<usize> = (0..inflight).collect();
    let mut queue: VecDeque<&Job> = jobs.iter().collect();
    let mut active = 0usize;
    let mut stats = RunStats {
        wall: Duration::ZERO,
        bytes: 0,
        reads: 0,
        lat: Vec::new(),
    };
    let mut reads = Vec::with_capacity(TOP_K * PARTS.len());
    let t0 = Instant::now();
    loop {
        while let Some(g) = free.last().copied() {
            let Some(job) = queue.pop_front() else { break };
            if job.experts.is_empty() {
                continue;
            }
            free.pop();
            reads.clear();
            for (k, &e) in job.experts.iter().enumerate() {
                let start = reads.len();
                layout.expert_reads(job.disk_layer, e as usize, &mut reads);
                for r in &mut reads[start..] {
                    r.dst += ((g * TOP_K + k) as u64) * STRIDE;
                }
            }
            started[g] = Instant::now();
            remaining[g] = reads.len();
            for r in &reads {
                let sqe = opcode::Read::new(fd, slots.at(0, r.dst), r.len as u32)
                    .offset(r.off)
                    .build()
                    .user_data(((g as u64) << 32) | r.len);
                // SAFETY: the buffer outlives the read; `slots` is dropped after the ring drains.
                unsafe { ring.submission().push(&sqe) }.map_err(|_| anyhow::anyhow!("SQ full"))?;
                stats.bytes += r.len;
            }
            stats.reads += reads.len() as u64;
            active += 1;
        }
        if active == 0 {
            break;
        }
        ring.submit_and_wait(1)?;
        let done: Vec<(u64, i32)> = ring
            .completion()
            .map(|c| (c.user_data(), c.result()))
            .collect();
        for (ud, res) in done {
            let (g, len) = ((ud >> 32) as usize, ud & 0xffff_ffff);
            if res < 0 {
                bail!("read failed: {}", std::io::Error::from_raw_os_error(-res));
            }
            ensure!(res as u64 == len, "short read: {res} of {len}");
            remaining[g] -= 1;
            if remaining[g] == 0 {
                stats.lat.push(started[g].elapsed());
                free.push(g);
                active -= 1;
            }
        }
    }
    stats.wall = t0.elapsed();
    Ok(stats)
}

struct PreadPool {
    tx: mpsc::Sender<Option<(usize, usize, u64, u64)>>,
    done_rx: mpsc::Receiver<std::io::Result<()>>,
    workers: Vec<thread::JoinHandle<()>>,
}

impl PreadPool {
    fn new(threads: usize) -> Self {
        let (tx, rx) = mpsc::channel::<Option<(usize, usize, u64, u64)>>();
        let rx = Arc::new(Mutex::new(rx));
        let (done_tx, done_rx) = mpsc::channel();
        let workers = (0..threads)
            .map(|_| {
                let rx = Arc::clone(&rx);
                let done_tx = done_tx.clone();
                thread::spawn(move || {
                    loop {
                        let msg = rx.lock().expect("pool lock").recv();
                        let Ok(Some((fd, ptr, off, len))) = msg else {
                            break;
                        };
                        // SAFETY: ptr points into a live slot buffer of at least len bytes.
                        let n = unsafe {
                            libc::pread(
                                fd as i32,
                                ptr as *mut libc::c_void,
                                len as usize,
                                off as i64,
                            )
                        };
                        let res = if n < 0 {
                            Err(std::io::Error::last_os_error())
                        } else if n as u64 != len {
                            Err(std::io::Error::other(format!("short read {n} of {len}")))
                        } else {
                            Ok(())
                        };
                        if done_tx.send(res).is_err() {
                            break;
                        }
                    }
                })
            })
            .collect();
        Self {
            tx,
            done_rx,
            workers,
        }
    }
}

impl Drop for PreadPool {
    fn drop(&mut self) {
        for _ in &self.workers {
            let _ = self.tx.send(None);
        }
        for w in self.workers.drain(..) {
            let _ = w.join();
        }
    }
}

/// One layer at a time over a blocking pread thread pool.
fn decode_pread(file: &File, layout: LayoutKind, jobs: &[Job], threads: usize) -> Result<RunStats> {
    let pool = PreadPool::new(threads);
    let slots = Slots::new(TOP_K);
    let fd = file.as_raw_fd() as usize;
    let mut stats = RunStats {
        wall: Duration::ZERO,
        bytes: 0,
        reads: 0,
        lat: Vec::new(),
    };
    let mut reads = Vec::new();
    let t0 = Instant::now();
    for job in jobs.iter().filter(|j| !j.experts.is_empty()) {
        reads.clear();
        for (k, &e) in job.experts.iter().enumerate() {
            let start = reads.len();
            layout.expert_reads(job.disk_layer, e as usize, &mut reads);
            for r in &mut reads[start..] {
                r.dst += k as u64 * STRIDE;
            }
        }
        let t = Instant::now();
        for r in &reads {
            pool.tx
                .send(Some((fd, slots.at(0, r.dst) as usize, r.off, r.len)))?;
            stats.bytes += r.len;
        }
        for _ in &reads {
            pool.done_rx.recv()??;
        }
        stats.lat.push(t.elapsed());
        stats.reads += reads.len() as u64;
    }
    stats.wall = t0.elapsed();
    drop(pool);
    Ok(stats)
}

// ---------------------------------------------------------------- stream

fn stream(a: &StreamArgs, disk_layers: usize) -> Result<String> {
    let file = open_direct(&a.dir, a.layout)?;
    let chunk = a.chunk_mib << 20;
    let layer_bytes = a.layout.layer_bytes();
    let slots = Slots::new(((chunk * a.qd as u64).div_ceil(STRIDE)) as usize);
    let fd = types::Fd(file.as_raw_fd());
    let mut per_run = Vec::new();
    for _ in 0..a.runs {
        let mut ring = IoUring::new(256)?;
        let t0 = Instant::now();
        for layer in 0..disk_layers {
            let base = layer as u64 * layer_bytes;
            let mut next = 0u64;
            let mut active = 0usize;
            let mut buf_free: Vec<usize> = (0..a.qd).collect();
            loop {
                while next < layer_bytes {
                    let Some(b) = buf_free.pop() else { break };
                    let len = chunk.min(layer_bytes - next);
                    let sqe = opcode::Read::new(fd, slots.at(0, b as u64 * chunk), len as u32)
                        .offset(base + next)
                        .build()
                        .user_data(((b as u64) << 32) | len);
                    // SAFETY: buffers outlive the ring's in-flight reads.
                    unsafe { ring.submission().push(&sqe) }
                        .map_err(|_| anyhow::anyhow!("SQ full"))?;
                    next += len;
                    active += 1;
                }
                if active == 0 {
                    break;
                }
                ring.submit_and_wait(1)?;
                let done: Vec<(u64, i32)> = ring
                    .completion()
                    .map(|c| (c.user_data(), c.result()))
                    .collect();
                for (ud, res) in done {
                    ensure!(
                        res >= 0 && res as u64 == ud & 0xffff_ffff,
                        "stream read failed: {res}"
                    );
                    buf_free.push((ud >> 32) as usize);
                    active -= 1;
                }
            }
        }
        per_run.push((disk_layers as u64 * layer_bytes) as f64 / t0.elapsed().as_secs_f64() / 1e9);
    }
    Ok(format!(
        "| {} | {} MiB x QD {} | {:.2} |",
        match a.layout {
            LayoutKind::Expert => "expert",
            LayoutKind::Tensor => "tensor",
        },
        a.chunk_mib,
        a.qd,
        median(per_run.into_iter())
    ))
}

// ---------------------------------------------------------------- interleaved A/B

/// Loads `experts` of `layer` into slots with every read submitted at once;
/// returns the time until the last completion.
fn load_once(
    ring: &mut IoUring,
    file: &File,
    layout: LayoutKind,
    slots: &Slots,
    layer: usize,
    experts: &[usize],
) -> Result<Duration> {
    let fd = types::Fd(file.as_raw_fd());
    let mut reads = Vec::new();
    for (k, &e) in experts.iter().enumerate() {
        let start = reads.len();
        layout.expert_reads(layer, e, &mut reads);
        for r in &mut reads[start..] {
            r.dst += k as u64 * STRIDE;
        }
    }
    let t = Instant::now();
    for r in &reads {
        let sqe = opcode::Read::new(fd, slots.at(0, r.dst), r.len as u32)
            .offset(r.off)
            .build()
            .user_data(r.len);
        // SAFETY: the slot buffer outlives the reads, which complete before return.
        unsafe { ring.submission().push(&sqe) }.map_err(|_| anyhow::anyhow!("SQ full"))?;
    }
    let mut left = reads.len();
    while left > 0 {
        ring.submit_and_wait(1)?;
        for c in ring.completion() {
            ensure!(
                c.result() >= 0 && c.result() as u64 == c.user_data(),
                "read failed: {}",
                c.result()
            );
            left -= 1;
        }
    }
    Ok(t.elapsed())
}

/// ABBA-interleaved samples so drift and background I/O hit both layouts alike.
fn ab(a: &AbArgs) -> Result<()> {
    let layers =
        file_layers(&a.dir, LayoutKind::Expert)?.min(file_layers(&a.dir, LayoutKind::Tensor)?);
    let files = [
        open_direct(&a.dir, LayoutKind::Expert)?,
        open_direct(&a.dir, LayoutKind::Tensor)?,
    ];
    let kinds = [LayoutKind::Expert, LayoutKind::Tensor];
    let max_k = a.k.iter().copied().max().unwrap_or(1);
    ensure!(max_k <= TOP_K, "k must be <= {TOP_K}");
    let slots = Slots::new(max_k);
    let mut ring = IoUring::new(256)?;
    let mut rng = StdRng::seed_from_u64(7);
    println!("| k | layout | p50 us | p95 us | p99 us | mean us | GB/s at p50 |");
    println!("|---|---|---|---|---|---|---|");
    let mut ratios = Vec::new();
    for &k in &a.k {
        let mut lat: [Vec<Duration>; 2] = [Vec::new(), Vec::new()];
        let mut paired = Vec::with_capacity(a.samples);
        for s in 0..a.samples {
            let layer = rng.random_range(0..layers);
            let mut experts: Vec<usize> = (0..NUM_EXPERTS).collect();
            experts.shuffle(&mut rng);
            experts.truncate(k);
            let order = if s % 2 == 0 { [0, 1] } else { [1, 0] };
            let mut pair = [Duration::ZERO; 2];
            for i in order {
                pair[i] = load_once(&mut ring, &files[i], kinds[i], &slots, layer, &experts)?;
                lat[i].push(pair[i]);
            }
            paired.push(pair[1].as_secs_f64() / pair[0].as_secs_f64());
        }
        for (i, samples) in lat.iter().enumerate() {
            let mut l = samples.clone();
            l.sort_unstable();
            let q = |f: f64| l[((l.len() - 1) as f64 * f).round() as usize].as_secs_f64() * 1e6;
            let mean = l.iter().map(Duration::as_secs_f64).sum::<f64>() / l.len() as f64 * 1e6;
            let bytes = k as f64 * PARTS.iter().sum::<u64>() as f64;
            println!(
                "| {k} | {} | {:.0} | {:.0} | {:.0} | {:.0} | {:.2} |",
                if i == 0 { "expert" } else { "tensor" },
                q(0.5),
                q(0.95),
                q(0.99),
                mean,
                bytes / (q(0.5) / 1e6) / 1e9
            );
        }
        ratios.push((k, median(paired.into_iter())));
    }
    println!("\n| k | median paired ratio tensor/expert |\n|---|---|");
    for (k, r) in ratios {
        println!("| {k} | {r:.3} |");
    }
    Ok(())
}

// ---------------------------------------------------------------- matrix

fn all(a: &AllArgs) -> Result<()> {
    prepare(&a.dir, a.layers)?;
    let result = run_matrix(a);
    if !a.keep {
        for layout in [LayoutKind::Expert, LayoutKind::Tensor] {
            let _ = fs::remove_file(a.dir.join(layout.file_name()));
        }
    }
    result
}

fn run_matrix(a: &AllArgs) -> Result<()> {
    let mut traces = Vec::new();
    if let Some(t) = &a.trace {
        traces.push(load_trace(&t.to_string_lossy())?);
    }
    traces.push(synthetic_trace(4096));

    println!(
        "## Decode misses ({} timed steps x 48 layers, median of {} runs)\n",
        a.steps, a.runs
    );
    print_decode_header();
    for trace in &traces {
        for cache in [0usize, 64, 128] {
            for (engine, inflight) in [(Engine::Uring, 1), (Engine::Uring, 4), (Engine::Pread, 1)] {
                for layout in [LayoutKind::Expert, LayoutKind::Tensor] {
                    let d = DecodeArgs {
                        dir: a.dir.clone(),
                        layout,
                        trace: String::new(),
                        steps: a.steps,
                        warmup: 256,
                        cache,
                        inflight,
                        engine,
                        threads: 10,
                        runs: a.runs,
                    };
                    let row = decode(&d, a.layers, trace)?;
                    println!("{}", row.markdown(&d));
                }
            }
        }
    }

    println!(
        "\n## Layer stream (all {} layers, median of {} runs)\n",
        a.layers, a.runs
    );
    println!("| layout | chunk x QD | GB/s |\n|---|---|---|");
    for layout in [LayoutKind::Expert, LayoutKind::Tensor] {
        for (chunk_mib, qd) in [(4, 8), (4, 16)] {
            let s = StreamArgs {
                dir: a.dir.clone(),
                layout,
                chunk_mib,
                qd,
                runs: a.runs,
            };
            println!("{}", stream(&s, a.layers)?);
        }
    }
    Ok(())
}
