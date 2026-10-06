//! Hardware profile: how fast this machine's drive, PCIe link, RAM and CPU are for
//! this engine's work, measured once and cached under
//! `$XDG_CACHE_HOME/oominf/` (or `~/.cache/oominf/`), keyed by a fingerprint of
//! the hardware, the model's storage and the engine.
//!
//! `serve` measures on start when no cached profile matches (a few seconds, just
//! before the model loads, on the same device); `oominf tune` measures longer and
//! writes the profile; `oominf doctor` shows it. Safety checks (usable memory,
//! free VRAM, the read path) are never cached: they run fresh on every start
//! (`crate::load`).
//!
//! The profile drives only a few conservative choices, each with its threshold
//! below ([`derive`]); a flag given on the command line always wins. Online
//! tuning while serving (adjusting these from measured steps, as the step cost
//! curve already is) would hook in at the engine's periodic report in
//! `oominf_server::engine::scheduler` and is not done yet.

use std::path::{Path, PathBuf};
use std::time::Instant;

use anyhow::{Context, Result};
use oominf_core::{Memory, Nvfp4Record, Transfer};
use oominf_cuda::Gpu;
use oominf_format::Model as Files;
use oominf_tiers::host::{DirectReader, IoConfig, ReadJob, ReadMode};
use serde::{Deserialize, Serialize};

use crate::load::{ExpertArgs, Tuning};

/// Bumped whenever what the probe measures or how [`derive`] uses it changes, so
/// older profiles are measured again.
const PROBE_VERSION: u32 = 1;

/// What a profile is valid for: a change in any field measures again.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Fingerprint {
    pub gpu: String,
    pub vram_bytes: u64,
    pub driver: String,
    pub cpu: String,
    /// CPUs this process may run on (affinity, so `taskset` counts).
    pub cpus: usize,
    pub ram_bytes: u64,
    /// The block device and filesystem holding the model's `experts.bin`.
    pub storage: String,
    pub engine: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Disk {
    /// The read path measured (the one the engine uses here).
    pub read_path: String,
    pub record_bytes: u64,
    /// Consecutive records, a few reads in flight (stage-ahead).
    pub sequential_gbps: f64,
    /// Records scattered over the file, many reads in flight (fetch misses).
    pub random_gbps: f64,
    /// One record at a time, median.
    pub record_latency_ms: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Profile {
    pub version: u32,
    pub fingerprint: Fingerprint,
    /// Unix seconds.
    pub measured_at: u64,
    pub probe_seconds: f64,
    /// Measured by `oominf tune` (more samples) rather than at start-up.
    pub thorough: bool,
    pub disk: Disk,
    /// Host-to-device copies from pinned memory.
    pub h2d_gbps: f64,
    /// One thread copying memory (bytes copied per second).
    pub ram_copy_gbps: f64,
    /// Host expert compute: threads, and microseconds per expert of a one-token
    /// step routing six experts (absent when the record layout has no host kernel).
    pub cpu_threads: usize,
    pub cpu_expert_us: Option<f64>,
}

/// How `serve` gets its profile.
#[derive(clap::Args, Debug, Clone, Default)]
pub struct ProfileArgs {
    /// Do not measure or read a hardware profile: use the built-in defaults.
    #[arg(long, conflicts_with_all = ["reprobe", "profile"])]
    pub no_probe: bool,
    /// Measure the hardware again even when a cached profile matches.
    #[arg(long)]
    pub reprobe: bool,
    /// Read the profile from this file (e.g. written by `oominf tune --out`)
    /// instead of the cache.
    #[arg(long)]
    pub profile: Option<PathBuf>,
}

const GB: f64 = 1e9;

/// Seconds of sequential reading the prefill staging ring needs to cover: about
/// two layers of a long prefill's compute (a 32k-token warm prefill spends about
/// 0.2 s per layer). Ring slots beyond what the drive fills in that time wait idle.
pub const RING_FILL_SECS: f64 = 0.5;
/// Fewest staging ring slots [`derive`] picks (one VRAM chunk's worth).
pub const RING_MIN: usize = 64;
/// Host compute is turned off by default when one expert on the CPU takes more
/// than this many times a record's PCIe copy: the GPU would then wait on the CPU
/// (on the reference machine the CPU takes 0.6 of a copy).
pub const HOST_COMPUTE_SLOWDOWN: f64 = 2.0;
/// Random record reads slower than this make cold prefill slow enough to warn.
pub const SLOW_DRIVE_GBPS: f64 = 1.0;
/// Reading the dense weights for longer than this makes start-up slow enough to
/// warn.
pub const SLOW_START_SECS: f64 = 60.0;
/// Host-to-device copies slower than this (a narrow or old PCIe link) make every
/// host-tier hit slow enough to warn.
pub const SLOW_PCIE_GBPS: f64 = 6.0;

/// Choices the profile justifies, with notes saying why and warnings for slow
/// hardware. `stride` and `num_experts` are the largest record layout's.
pub fn derive(p: &Profile, files: &Files) -> Result<(Tuning, Vec<String>, Vec<String>)> {
    let layouts = oominf_tiers::layouts(files)?;
    let main = &layouts[oominf_tiers::plan::largest(&layouts)];
    let (stride, ne) = (main.stride as f64, main.num_experts);
    let mut t = Tuning::default();
    let (mut notes, mut warnings) = (Vec::new(), Vec::new());
    // Rule 1: host compute off when the CPU is clearly slower than a copy.
    let copy_us = stride / (p.h2d_gbps * GB) * 1e6;
    if let Some(cpu) = p.cpu_expert_us
        && cpu > HOST_COMPUTE_SLOWDOWN * copy_us
    {
        t.host_compute = Some(0);
        notes.push(format!(
            "host compute off: an expert takes {cpu:.0} us on {} CPU threads against {copy_us:.0} us to copy its record (more than {HOST_COMPUTE_SLOWDOWN}x)",
            p.cpu_threads
        ));
    }
    // Rule 2: a staging ring no larger than the drive fills in RING_FILL_SECS.
    let fill = (p.disk.sequential_gbps * GB * RING_FILL_SECS / stride).ceil() as usize;
    let ring = fill.clamp(RING_MIN.min(ne), ne);
    if ring < ne {
        t.host_stage_slots = Some(ring);
        notes.push(format!(
            "prefill staging ring {ring} of {ne} records: the drive fills about {fill} in {RING_FILL_SECS} s; {:.1} GiB go to the host tier instead",
            ((ne - ring) as f64 * stride) / (1u64 << 30) as f64
        ));
    }
    // Warnings: slow drive (cold prefill, start-up) and slow PCIe.
    if p.disk.random_gbps < SLOW_DRIVE_GBPS {
        let sweep = layouts
            .iter()
            .map(|l| (l.records * l.stride) as f64)
            .sum::<f64>();
        warnings.push(format!(
            "slow drive: {:.2} GB/s for scattered record reads; a cold prefill that sweeps every expert reads up to {:.0} GB, about {:.0} s, and decode misses wait {:.1} ms each",
            p.disk.random_gbps,
            sweep / GB,
            sweep / (p.disk.random_gbps * GB),
            p.disk.record_latency_ms
        ));
    }
    let dense = files.index().files.dense.bytes as f64;
    let start = dense / (p.disk.sequential_gbps * GB);
    if start > SLOW_START_SECS {
        warnings.push(format!(
            "slow start-up: reading {:.1} GB of dense weights takes about {start:.0} s at {:.2} GB/s",
            dense / GB,
            p.disk.sequential_gbps
        ));
    }
    if p.h2d_gbps < SLOW_PCIE_GBPS {
        warnings.push(format!(
            "slow PCIe: {:.1} GB/s host to device; every host-tier hit waits {copy_us:.0} us for its copy",
            p.h2d_gbps
        ));
    }
    Ok((t, notes, warnings))
}

/// The cache directory: `$XDG_CACHE_HOME/oominf`, else `~/.cache/oominf`.
pub fn cache_dir() -> Option<PathBuf> {
    let base = std::env::var_os("XDG_CACHE_HOME")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".cache")))?;
    Some(base.join("oominf"))
}

impl Fingerprint {
    pub fn collect(gpu: &Gpu, files: &Files) -> Result<Self> {
        let (_, vram) = gpu.mem_info()?;
        let cpuinfo = std::fs::read_to_string("/proc/cpuinfo").unwrap_or_default();
        let cpu = cpuinfo
            .lines()
            .find_map(|l| l.strip_prefix("model name"))
            .and_then(|l| l.split_once(':'))
            .map_or("unknown".into(), |(_, v)| v.trim().to_owned());
        let mem = oominf_tiers::resources::HostMemory::probe()?;
        Ok(Fingerprint {
            gpu: gpu.name()?,
            vram_bytes: vram as u64,
            driver: driver_version(),
            cpu,
            cpus: std::thread::available_parallelism().map_or(1, |n| n.get()),
            ram_bytes: mem.total,
            storage: storage_of(&files.dir().join(oominf_format::EXPERTS_FILE)),
            engine: format!("{} probe {PROBE_VERSION}", env!("CARGO_PKG_VERSION")),
        })
    }

    /// The cache file name for this fingerprint.
    pub fn file_name(&self) -> String {
        let json = serde_json::to_vec(self).expect("serializable");
        format!("profile-{}.json", &oominf_format::checksum(&json)[..16])
    }
}

/// The NVIDIA kernel driver's version, from `/proc/driver/nvidia/version`.
fn driver_version() -> String {
    std::fs::read_to_string("/proc/driver/nvidia/version")
        .ok()
        .and_then(|s| {
            s.split_whitespace()
                .find(|w| {
                    w.split('.').count() >= 2 && w.split('.').all(|p| p.parse::<u32>().is_ok())
                })
                .map(str::to_owned)
        })
        .unwrap_or_else(|| "unknown".into())
}

/// `device fstype model` of the filesystem holding `path` (from
/// `/proc/self/mountinfo` and `/sys/dev/block`).
fn storage_of(path: &Path) -> String {
    use std::os::unix::fs::MetadataExt;
    let Ok(meta) = std::fs::metadata(path) else {
        return "unknown".into();
    };
    let dev = meta.dev();
    let (major, minor) = (libc::major(dev), libc::minor(dev));
    let mountinfo = std::fs::read_to_string("/proc/self/mountinfo").unwrap_or_default();
    // Fields: id parent major:minor root mountpoint options ... - fstype source ...
    let (fstype, source) = mountinfo
        .lines()
        .find_map(|l| {
            let f: Vec<&str> = l.split_whitespace().collect();
            if f.get(2)? != &format!("{major}:{minor}").as_str() {
                return None;
            }
            let dash = f.iter().position(|&x| x == "-")?;
            Some((f.get(dash + 1)?.to_string(), f.get(dash + 2)?.to_string()))
        })
        .unwrap_or(("unknown".into(), "unknown".into()));
    let sys = PathBuf::from(format!("/sys/dev/block/{major}:{minor}"));
    // A partition's model lives on its parent device.
    let model = ["device/model", "../device/model"]
        .iter()
        .find_map(|p| std::fs::read_to_string(sys.join(p)).ok())
        .map_or("unknown".into(), |m| m.trim().to_owned());
    format!("{source} {fstype} {model}")
}

impl Profile {
    pub fn load(path: &Path) -> Result<Profile> {
        let bytes = std::fs::read(path).with_context(|| format!("read {}", path.display()))?;
        serde_json::from_slice(&bytes).with_context(|| format!("parse {}", path.display()))
    }

    pub fn save(&self, path: &Path) -> Result<()> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let tmp = path.with_extension("json.tmp");
        std::fs::write(&tmp, serde_json::to_vec_pretty(self)?)?;
        std::fs::rename(&tmp, path)?;
        Ok(())
    }

    pub fn summary(&self) -> String {
        format!(
            "disk ({}) sequential {:.2} GB/s, scattered {:.2} GB/s, {:.2} ms per record; PCIe host to device {:.1} GB/s; RAM copy {:.1} GB/s; CPU expert {} on {} threads",
            self.disk.read_path,
            self.disk.sequential_gbps,
            self.disk.random_gbps,
            self.disk.record_latency_ms,
            self.h2d_gbps,
            self.ram_copy_gbps,
            self.cpu_expert_us
                .map_or("n/a".into(), |u| format!("{u:.0} us")),
            self.cpu_threads
        )
    }
}

/// Where `serve` gets the profile, and how it was obtained.
pub enum Source {
    Cached(PathBuf),
    Measured(PathBuf),
    File(PathBuf),
}

/// The profile for `serve`: a given file, else the cached one matching this
/// machine, else measured now (and cached). `None` with `--no-probe`.
pub fn resolve(
    args: &ProfileArgs,
    gpu: &Gpu,
    files: &Files,
    experts: &ExpertArgs,
) -> Result<Option<(Profile, Source)>> {
    if args.no_probe {
        return Ok(None);
    }
    if let Some(p) = &args.profile {
        return Ok(Some((Profile::load(p)?, Source::File(p.clone()))));
    }
    let fp = Fingerprint::collect(gpu, files)?;
    let path = cache_dir().map(|d| d.join(fp.file_name()));
    if !args.reprobe
        && let Some(path) = &path
        && let Ok(p) = Profile::load(path)
        && p.version == PROBE_VERSION
        && p.fingerprint == fp
    {
        return Ok(Some((p, Source::Cached(path.clone()))));
    }
    let p = measure(gpu, files, experts, fp, false)?;
    let Some(path) = path else {
        eprintln!("oominf: warning: no cache directory (HOME unset); the profile is not saved");
        return Ok(Some((p, Source::Measured(PathBuf::new()))));
    };
    if let Err(e) = p.save(&path) {
        eprintln!(
            "oominf: warning: cannot save the profile to {}: {e:#}",
            path.display()
        );
    }
    Ok(Some((p, Source::Measured(path))))
}

/// The tuning `serve` applies: the profile's choices (see [`derive`]), logged with
/// why; built-in defaults without a profile.
pub fn tuning(
    args: &ProfileArgs,
    gpu: &Gpu,
    files: &Files,
    experts: &ExpertArgs,
) -> Result<Tuning> {
    let Some((p, source)) = resolve(args, gpu, files, experts)? else {
        eprintln!("oominf: hardware profile skipped (--no-probe): built-in defaults");
        return Ok(Tuning::default());
    };
    let how = match &source {
        Source::Cached(path) => format!("cached {}", path.display()),
        Source::Measured(path) => format!(
            "measured in {:.1} s, saved to {}",
            p.probe_seconds,
            path.display()
        ),
        Source::File(path) => format!("from {}", path.display()),
    };
    eprintln!("oominf: hardware profile ({how}): {}", p.summary());
    let (t, notes, warnings) = derive(&p, files)?;
    for n in notes {
        eprintln!("oominf: profile: {n}");
    }
    for w in warnings {
        eprintln!("oominf: warning: {w}");
    }
    Ok(t)
}

/// Page-aligned anonymous memory.
struct Region(*mut u8, usize);

impl Region {
    fn new(len: usize) -> Result<Region> {
        // SAFETY: anonymous private mapping, checked below.
        let p = unsafe {
            libc::mmap(
                std::ptr::null_mut(),
                len,
                libc::PROT_READ | libc::PROT_WRITE,
                libc::MAP_PRIVATE | libc::MAP_ANONYMOUS | libc::MAP_POPULATE,
                -1,
                0,
            )
        };
        anyhow::ensure!(p != libc::MAP_FAILED, "mmap of {len} bytes failed");
        Ok(Region(p.cast(), len))
    }

    fn slice(&self) -> &[u8] {
        // SAFETY: the mapping holds `len` bytes.
        unsafe { std::slice::from_raw_parts(self.0, self.1) }
    }
}

impl Drop for Region {
    fn drop(&mut self) {
        // SAFETY: mapped in `new`.
        unsafe { libc::munmap(self.0.cast(), self.1) };
    }
}

/// Reads `records` (offset, length) into `buf` with `depth` reads in flight;
/// returns seconds.
fn timed_reads(
    path: &Path,
    io: &IoConfig,
    depth: u32,
    records: &[(u64, usize)],
    buf: &Region,
) -> Result<f64> {
    let mut reader = DirectReader::open_with(path, depth, io)?;
    let mut at = 0;
    let jobs = records
        .iter()
        .enumerate()
        .map(|(tag, &(offset, len))| {
            assert!(at + len <= buf.1);
            // SAFETY: within `buf`.
            let dst = unsafe { buf.0.add(at) };
            at += len;
            ReadJob {
                offset,
                dst,
                len,
                tag,
            }
        })
        .collect();
    let t = Instant::now();
    reader.submit(jobs)?;
    reader.drain(|_| Ok(()))?;
    Ok(t.elapsed().as_secs_f64())
}

/// A deterministic pseudo-random sequence.
struct Lcg(u64);

impl Lcg {
    fn below(&mut self, n: u64) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        (self.0 >> 33) % n.max(1)
    }
}

fn median(mut v: Vec<f64>) -> f64 {
    v.sort_by(f64::total_cmp);
    v[v.len() / 2]
}

/// Measures the profile: `thorough` (for `oominf tune`) takes four times the
/// samples and the median of three rounds.
pub fn measure(
    gpu: &Gpu,
    files: &Files,
    experts: &ExpertArgs,
    fingerprint: Fingerprint,
    thorough: bool,
) -> Result<Profile> {
    let t0 = Instant::now();
    let path = files.dir().join(oominf_format::EXPERTS_FILE);
    let mode = crate::load::read_mode(experts, files)?;
    let io = IoConfig::new(mode);
    let layouts = oominf_tiers::layouts(files)?;
    let main = &layouts[oominf_tiers::plan::largest(&layouts)];
    let groups: Vec<_> = files
        .index()
        .expert_groups
        .iter()
        .filter(|g| g.schema.layout == main.name)
        .collect();
    let (n, rounds) = if thorough { (192, 3) } else { (48, 1) };
    let stride = main.stride;
    let buf = Region::new(n * stride)?;
    if mode == ReadMode::Buffered {
        // Earlier reads must not be served from the page cache.
        if let Ok(f) = std::fs::File::open(&path) {
            use std::os::fd::AsRawFd;
            // SAFETY: advice on an open descriptor.
            unsafe { libc::posix_fadvise(f.as_raw_fd(), 0, 0, libc::POSIX_FADV_DONTNEED) };
        }
    }
    let mut rng = Lcg(fingerprint.cpus as u64 ^ t0.elapsed().subsec_nanos() as u64);
    let (mut seq, mut rand, mut lat) = (Vec::new(), Vec::new(), Vec::new());
    for _ in 0..rounds {
        // Consecutive records of one layer, starting where they fit.
        let g = groups[rng.below(groups.len() as u64) as usize];
        let first = rng.below(
            (g.num_experts as usize).saturating_sub(n.min(g.num_experts as usize)) as u64 + 1,
        );
        let records: Vec<_> = (0..n.min(g.num_experts as usize) as u64)
            .map(|i| (g.record_offset((first + i) as u32), stride))
            .collect();
        let secs = timed_reads(&path, &io, 4, &records, &buf)?;
        seq.push((records.len() * stride) as f64 / secs / GB);
        // Records scattered over every layer.
        let scattered: Vec<_> = (0..n)
            .map(|_| {
                let g = groups[rng.below(groups.len() as u64) as usize];
                (
                    g.record_offset(rng.below(g.num_experts as u64) as u32),
                    stride,
                )
            })
            .collect();
        let secs = timed_reads(&path, &io, 32, &scattered, &buf)?;
        rand.push((n * stride) as f64 / secs / GB);
        for _ in 0..n / 6 {
            let g = groups[rng.below(groups.len() as u64) as usize];
            let one = [(
                g.record_offset(rng.below(g.num_experts as u64) as u32),
                stride,
            )];
            lat.push(timed_reads(&path, &io, 1, &one, &buf)? * 1e3);
        }
    }
    let disk = Disk {
        read_path: mode.name().into(),
        record_bytes: stride as u64,
        sequential_gbps: median(seq),
        random_gbps: median(rand),
        record_latency_ms: median(lat),
    };
    let h2d_gbps = h2d(gpu, if thorough { 16 } else { 4 })?;
    let ram_copy_gbps = ram_copy(if thorough { 8 } else { 3 })?;
    let cpu_threads = experts.host_threads.unwrap_or_else(|| {
        std::thread::available_parallelism().map_or(4, |n| (n.get() / 2).max(1))
    });
    // The records just read are real expert weights for the CPU kernel.
    let cpu_expert_us = cpu_expert(files, &main.name, &buf, n, cpu_threads, thorough)?;
    Ok(Profile {
        version: PROBE_VERSION,
        fingerprint,
        measured_at: std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |d| d.as_secs()),
        probe_seconds: t0.elapsed().as_secs_f64(),
        thorough,
        disk,
        h2d_gbps,
        ram_copy_gbps,
        cpu_threads,
        cpu_expert_us,
    })
}

/// Host-to-device copy rate from pinned memory: `reps` copies of 64 MiB, best.
fn h2d(gpu: &Gpu, reps: usize) -> Result<f64> {
    let len = 64 << 20;
    let host = Region::new(len)?;
    // SAFETY: the region outlives its registration (unpinned below).
    unsafe { gpu.pin_host(host.0, len)? };
    let res = (|| -> Result<f64> {
        let dev = gpu.zeros_bytes(len)?;
        let q = gpu.copy_queue()?;
        let mut best = f64::MAX;
        for i in 0..=reps {
            let t = Instant::now();
            // SAFETY: both buffers live until the copy completes (waited below).
            unsafe { gpu.copy_to_device(&q, gpu.bytes_addr(&dev), host.0, len)? };
            gpu.event_wait(&gpu.record_copies(&q)?)?;
            // The first copy warms the link up.
            if i > 0 {
                best = best.min(t.elapsed().as_secs_f64());
            }
        }
        Ok(len as f64 / best / GB)
    })();
    // SAFETY: pinned above; every copy from it completed.
    unsafe { gpu.unpin_host(host.0) };
    res
}

/// One thread's memory copy rate over 256 MiB, best of `reps`.
fn ram_copy(reps: usize) -> Result<f64> {
    let len = 256 << 20;
    let src = Region::new(len)?;
    let dst = Region::new(len)?;
    let mut best = f64::MAX;
    for _ in 0..reps {
        let t = Instant::now();
        // SAFETY: two distinct mappings of `len` bytes.
        unsafe { std::ptr::copy_nonoverlapping(src.0, dst.0, len) };
        best = best.min(t.elapsed().as_secs_f64());
    }
    std::hint::black_box(dst.slice()[len / 2]);
    Ok(len as f64 / best / GB)
}

/// Microseconds per expert of a one-token decode step routing six experts on the
/// host-expert pool, over records of `layout` in `buf` (`n` of them); `None` for a
/// layout without a host kernel.
fn cpu_expert(
    files: &Files,
    layout: &str,
    buf: &Region,
    n: usize,
    threads: usize,
    thorough: bool,
) -> Result<Option<f64>> {
    let Some(g) = files
        .index()
        .expert_groups
        .iter()
        .find(|g| g.schema.layout == layout)
    else {
        return Ok(None);
    };
    let s = &g.schema;
    let (Some(gw), Some(gs), Some(uw), Some(us), Some(dw), Some(ds)) = (
        s.part("gate.weight"),
        s.part("gate.weight_scale"),
        s.part("up.weight"),
        s.part("up.weight_scale"),
        s.part("down.weight"),
        s.part("down.weight_scale"),
    ) else {
        return Ok(None);
    };
    if layout != "nvfp4-modelopt-g16" {
        return Ok(None);
    }
    let geo = Nvfp4Record {
        hidden: 2 * gw.shape[1] as usize,
        inter: gw.shape[0] as usize,
        gate_weight: gw.offset as usize,
        gate_scale: gs.offset as usize,
        up_weight: uw.offset as usize,
        up_scale: us.offset as usize,
        down_weight: dw.offset as usize,
        down_scale: ds.offset as usize,
        scale2: [0, 2, 4],
    };
    let pool = oominf_cpu::HostExperts::new(threads)?;
    let stride = s.stride as usize;
    const EXPERTS: usize = 6;
    let steps = if thorough { 60 } else { 15 };
    let mut times = Vec::with_capacity(steps);
    for step in 0..=steps {
        let jobs = (0..EXPERTS)
            .map(|e| oominf_cpu::Job {
                record: buf.0 as usize + ((step * EXPERTS + e) % n) * stride,
                len: stride,
                tokens: vec![0],
            })
            .collect();
        let mut x = pool.buffer(geo.hidden);
        x.iter_mut()
            .enumerate()
            .for_each(|(i, v)| *v = (i % 13) as f32 * 0.01);
        let t = Instant::now();
        // SAFETY: the records in `buf` stay unchanged until the wait returns.
        let y = unsafe { pool.submit(oominf_cpu::Geometry::Nvfp4(geo), x, jobs) }.wait()?;
        // The first step warms the pool up.
        if step > 0 {
            times.push(t.elapsed().as_secs_f64());
        }
        pool.recycle(y);
    }
    Ok(Some(median(times) * 1e6 / EXPERTS as f64))
}

/// Prints the profile and what it decides (`tune` and `doctor`).
pub fn report(p: &Profile, files: &Files) -> Result<()> {
    println!("profile: {}", p.summary());
    println!(
        "  measured {} ({:.1} s{})",
        p.measured_at,
        p.probe_seconds,
        if p.thorough { ", thorough" } else { "" }
    );
    let (t, notes, warnings) = derive(p, files)?;
    if notes.is_empty() {
        println!("  decisions: none (built-in defaults fit this machine)");
    }
    for n in notes {
        println!("  decision: {n}");
    }
    for w in warnings {
        println!("  warning: {w}");
    }
    let _ = t;
    Ok(())
}
