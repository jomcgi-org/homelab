//! Opening a model for the commands that run it: the CUDA backend, the model
//! registry and the expert source chosen on the command line.

use std::path::Path;
use std::sync::Arc;

use anyhow::Result;
use oominf_core::{Backend, ExpertFactory, ExpertSource, HostDemand, Model};
#[cfg(not(target_os = "macos"))]
use oominf_cuda::Gpu;
use oominf_format::Model as Files;
use oominf_tiers::host::{IoConfig, ReadMode};
use oominf_tiers::resources::HostMemory;
use oominf_tiers::{DiskExperts, TierBudget, policy};

#[derive(clap::Args, Debug, Clone)]
pub struct ExpertArgs {
    /// Where routed experts come from: `tiered` (VRAM, pinned host, disk) or `disk`
    /// (read and upload every record, no cache).
    #[arg(long, default_value = "tiered")]
    pub experts: String,
    /// VRAM budget for cached expert records (default: free VRAM once the model and
    /// a first sequence are loaded, minus `--vram-reserve-gib`). Shrunk with a
    /// warning when it does not fit.
    #[arg(long)]
    pub vram_expert_gib: Option<f64>,
    /// VRAM left free for activations and later allocations when sizing
    /// automatically [default: 2; lowered to 1 when the smallest working tier
    /// needs it, unless given].
    #[arg(long)]
    pub vram_reserve_gib: Option<f64>,
    /// Pinned host-memory budget for cached expert records and the prefill staging
    /// ring (default: usable RAM, the smaller of `MemAvailable` and the control
    /// group's headroom, minus `--host-reserve-gib` and what sequences keep in host
    /// memory). Shrunk with a warning when it does not fit.
    #[arg(long)]
    pub host_expert_gib: Option<f64>,
    /// RAM left available (page cache for embedding-table rows, the OS, other
    /// processes) when sizing automatically [default: 10; lowered as far as 3 when
    /// the smallest working tier needs it, unless given].
    #[arg(long)]
    pub host_reserve_gib: Option<f64>,
    /// VRAM tier policy: `lru`, `lfu` or `lrfu:<half-life in expert accesses>`.
    #[arg(long, default_value = "lrfu:7680")]
    pub vram_policy: String,
    /// Host tier policy (same syntax).
    #[arg(long, default_value = "lrfu:30720")]
    pub host_policy: String,
    /// During decode, predict the next layer's experts and start reading predicted
    /// disk misses into the host tier: `on` or `off`. Off by default: on the served
    /// demo only about a quarter of those reads were used, and the rest cost disk
    /// bandwidth and host-tier slots (see ARCHITECTURE.md, "Why no decode lookahead").
    #[arg(long, default_value = "off")]
    pub lookahead: String,
    /// Most routed experts per layer and decode step computed on the CPU from host
    /// memory instead of copied to the device (0 copies every one) [default: 6, or 0
    /// when the hardware profile finds the CPU much slower than a PCIe copy].
    #[arg(long)]
    pub host_compute: Option<usize>,
    /// CPU threads for those experts (default: one per physical core).
    #[arg(long)]
    pub host_threads: Option<usize>,
    /// How expert records are read from disk: `auto` (the fastest that works
    /// here), `uring` (O_DIRECT through io_uring), `pread` (O_DIRECT pread on a
    /// thread pool) or `buffered` (pread through the page cache, for filesystems
    /// without O_DIRECT).
    #[arg(long, default_value = "auto")]
    pub io: String,
}

/// Runtime choices for a model's sequences, beyond where experts come from.
#[derive(clap::Args, Debug, Clone)]
pub struct CacheArgs {
    /// How attention layers store their KV cache: compressed TurboQuant-style as
    /// `k<bits>v<bits>` or `tq<bits>` (2 to 8 bits per coordinate), or `fp32` (exact).
    /// The default `k8v6` measured inside fp32's own rounding noise at 32k-95k tokens
    /// while keeping about 3x less KV memory (#6830); other modes are lossy to
    /// different degrees: judge them with `oominf score`.
    #[cfg_attr(target_os = "macos", arg(long, default_value = "fp32", value_parser = oominf_core::KvFormat::parse))]
    #[cfg_attr(not(target_os = "macos"), arg(long, default_value = "k8v6", value_parser = oominf_core::KvFormat::parse))]
    pub kv_cache: oominf_core::KvFormat,
    /// How dense (non-expert) weights are stored: `bf16` as in the checkpoint
    /// (exact), or `fp8` (e4m3 with a scale per 128 weights: half the bytes, so faster
    /// decode and more VRAM for experts; lossy, judge with `oominf score`).
    #[arg(long, default_value = "bf16", value_parser = oominf_core::DenseFormat::parse)]
    pub dense: oominf_core::DenseFormat,
    /// Activation precision of the prefill routed-expert GEMM: `exact` (fp32 as three
    /// exact bf16 terms) or `bf16` (activations rounded to bf16, one tensor-core pass:
    /// faster prefill; lossy, judge with `oominf score`). Decode is unaffected.
    #[arg(long, default_value = "exact", value_parser = oominf_core::ExpertPrecision::parse)]
    pub expert_precision: oominf_core::ExpertPrecision,
    /// Arithmetic of prefill attention: `exact` (fp32) or `bf16` (tensor cores with
    /// bf16 queries, keys and values and fp32 accumulation: faster prefill; lossy,
    /// judge with `oominf score`). Decode attention is fp32 either way.
    #[arg(long, default_value = "exact", value_parser = oominf_core::AttentionPrecision::parse)]
    pub attention_precision: oominf_core::AttentionPrecision,
    /// Where attention K/V caches live: `device` memory, or `host` memory the GPU
    /// reads over PCIe (decode reads only each token's selected rows; frees VRAM
    /// for experts at long context; same results).
    #[arg(long, default_value = "device", value_parser = ["device", "host"])]
    pub kv_placement: String,
}

impl CacheArgs {
    pub fn kv_host(&self) -> bool {
        self.kv_placement == "host"
    }
}

/// Host-compute experts per layer and decode step unless the profile or the
/// command line says otherwise.
pub const DEFAULT_HOST_COMPUTE: usize = 6;
/// Before the staging ring and transfer buffers were counted beside the tiers, the
/// default reserve was 12 GiB with those inside it; 10 keeps the same memory free.
const DEFAULT_HOST_RESERVE_GIB: f64 = 10.0;
const DEFAULT_VRAM_RESERVE_GIB: f64 = 2.0;
/// An allowance for host memory outside the tiers that does not grow with the
/// context: pinned upload and download buffers (a ring of 64, at most 1 MiB
/// each), routing tables and the host-expert pool's work buffers.
const TRANSFER_BUFFERS: u64 = 512 << 20;
/// Prefix-store snapshots that can be in host memory at once: one being built on
/// the engine thread, one queued and one being written.
const SNAPSHOTS_IN_FLIGHT: u64 = 3;

/// Values derived from a hardware profile ([`crate::profile`]); a flag given on the
/// command line always wins over them.
#[derive(Debug, Clone, Default)]
pub struct Tuning {
    pub host_compute: Option<usize>,
    pub host_stage_slots: Option<usize>,
}

/// What the model's sequences keep in host memory beside the tiers, from the
/// command's settings.
#[derive(Debug, Clone, Copy)]
pub struct HostUse {
    /// Sequences alive at once (decoding plus cached).
    pub sequences: usize,
    /// Prefix checkpoints one full-context sequence keeps.
    pub checkpoints: usize,
    /// A prefix store saves evicted sequences.
    pub snapshots: bool,
}

impl Default for HostUse {
    fn default() -> Self {
        HostUse {
            sequences: 1,
            checkpoints: 0,
            snapshots: false,
        }
    }
}

impl ExpertArgs {
    /// Experts per layer and step computed on the host: the flag, else the
    /// profile's, else the default.
    pub fn host_compute(&self, tuning: &Tuning) -> usize {
        self.host_compute
            .or(tuning.host_compute)
            .unwrap_or(DEFAULT_HOST_COMPUTE)
    }

    /// CPU threads computing host-resident experts (0 when host compute is off).
    pub fn host_threads(&self, tuning: &Tuning) -> usize {
        if self.host_compute(tuning) == 0 {
            return 0;
        }
        self.host_threads.unwrap_or_else(|| {
            std::thread::available_parallelism().map_or(4, |n| (n.get() / 2).max(1))
        })
    }
}

const GIB: f64 = (1u64 << 30) as f64;

fn bytes(gib: f64) -> u64 {
    (gib.max(0.0) * GIB) as u64
}

/// The tiers' budget, with the figures it came from and warnings for anything
/// shrunk.
pub struct BudgetReport {
    pub budget: TierBudget,
    pub lines: Vec<String>,
    pub warnings: Vec<String>,
}

/// The expert tiers' memory: fresh checks of free VRAM and usable host memory,
/// split by [`oominf_tiers::plan`]. `loading` is host memory the model will still
/// take before the tiers are sized (0 once it has loaded; an estimate for
/// `doctor`).
#[allow(clippy::too_many_arguments)]
pub fn tier_budget(
    args: &ExpertArgs,
    files: &Files,
    free_vram: u64,
    loading: u64,
    demand: &HostDemand,
    host_use: &HostUse,
    tuning: &Tuning,
) -> Result<BudgetReport> {
    use oominf_tiers::plan;
    let layouts = oominf_tiers::layouts(files)?;
    let vram_floors = plan::floors(&layouts, false);
    let v = plan::plan(
        "VRAM",
        &plan::Request {
            usable: free_vram,
            reserve: bytes(args.vram_reserve_gib.unwrap_or(DEFAULT_VRAM_RESERVE_GIB)),
            reserve_explicit: args.vram_reserve_gib.is_some(),
            min_reserve: plan::MIN_VRAM_RESERVE,
            requested: args.vram_expert_gib.map(bytes),
            outside: Vec::new(),
            floor: vram_floors.vram,
            useful: vram_floors.vram_useful,
        },
    )
    .map_err(|e| {
        anyhow::anyhow!(
            "{e}; to fit, free GPU memory, use --dense fp8, lower --max-context, or use --kv-placement host"
        )
    })?;
    let mem = HostMemory::probe()?;
    let mut lines = vec![mem.describe()];
    let stage = v.bytes >= plan::stage_bytes(&layouts);
    let host_floors = plan::floors(&layouts, stage);
    let n = host_use.sequences as u64;
    let mut outside = vec![("transfer and work buffers".to_owned(), TRANSFER_BUFFERS)];
    let mut add = |what: &str, b: u64| {
        if b > 0 {
            outside.push((what.to_owned(), b));
        }
    };
    add("host KV caches", demand.kv as u64 * n);
    let checkpoints = (demand.checkpoint * host_use.checkpoints) as u64;
    add("prefix checkpoints", checkpoints * n);
    if host_use.snapshots {
        add(
            "prefix store snapshots",
            (demand.snapshot as u64 + checkpoints) * SNAPSHOTS_IN_FLIGHT,
        );
    }
    let h = plan::plan(
        "host memory",
        &plan::Request {
            usable: mem.usable().saturating_sub(loading),
            reserve: bytes(args.host_reserve_gib.unwrap_or(DEFAULT_HOST_RESERVE_GIB)),
            reserve_explicit: args.host_reserve_gib.is_some(),
            min_reserve: plan::MIN_HOST_RESERVE,
            requested: args.host_expert_gib.map(bytes),
            outside: outside.clone(),
            floor: host_floors.host,
            useful: host_floors.host_useful,
        },
    )
    .map_err(|e| {
        anyhow::anyhow!(
            "{e}; to fit, raise the memory (or container) limit, lower --max-context or --max-streams (fewer prefix checkpoints), drop --prefix-store-dir or --kv-placement host, or lower --host-reserve-gib"
        )
    })?;
    let detail: Vec<String> = outside
        .iter()
        .map(|(w, b)| format!("{w} {:.1}", *b as f64 / GIB))
        .collect();
    lines.push(format!(
        "expert tiers: VRAM {:.1} GiB of {:.1} free (reserve {:.1}); host {:.1} GiB of {:.1} usable (reserve {:.1}; beside the tiers, GiB: {})",
        v.bytes as f64 / GIB,
        free_vram as f64 / GIB,
        v.reserve as f64 / GIB,
        h.bytes as f64 / GIB,
        mem.usable() as f64 / GIB,
        h.reserve as f64 / GIB,
        detail.join(", ")
    ));
    Ok(BudgetReport {
        budget: TierBudget {
            vram_gib: v.bytes as f64 / GIB,
            host_gib: h.bytes as f64 / GIB,
            host_stage_slots: tuning.host_stage_slots,
        },
        lines,
        warnings: v.warnings.into_iter().chain(h.warnings).collect(),
    })
}

/// The read path for the model's `experts.bin`: `--io`, or the fastest that works.
/// A fresh check on every start; logs which path is active and why faster ones
/// were skipped.
pub fn read_mode(args: &ExpertArgs, files: &Files) -> Result<ReadMode> {
    let path = files.dir().join(oominf_format::EXPERTS_FILE);
    let (mode, skipped) = oominf_tiers::host::select_read_mode(&path, ReadMode::parse(&args.io)?)?;
    for s in &skipped {
        eprintln!("oominf: warning: expert read path unavailable, {s}");
    }
    eprintln!("oominf: expert reads: {}", mode.describe());
    Ok(mode)
}

/// Builds the expert source `args` selects, once the model is on the device.
pub fn factory<B: Backend>(
    args: &ExpertArgs,
    files: Arc<Files>,
    host_use: HostUse,
    tuning: &Tuning,
) -> ExpertFactory<B> {
    let args = args.clone();
    let tuning = tuning.clone();
    Box::new(
        move |b: &Arc<B>, demand: &HostDemand| -> Result<Box<dyn ExpertSource<B>>> {
            Ok(match args.experts.as_str() {
                "disk" => Box::new(DiskExperts::new(files)),
                "tiered" => {
                    let io = IoConfig::new(read_mode(&args, &files)?);
                    let (free, _) = b.mem_info()?;
                    let report =
                        tier_budget(&args, &files, free as u64, 0, demand, &host_use, &tuning)?;
                    for l in &report.lines {
                        eprintln!("oominf: {l}");
                    }
                    for w in &report.warnings {
                        eprintln!("oominf: warning: {w}");
                    }
                    let budget = report.budget;
                    let lookahead = match args.lookahead.as_str() {
                        "on" => true,
                        "off" => false,
                        other => anyhow::bail!("unknown --lookahead {other:?} (on, off)"),
                    };
                    let policies = || -> Result<_> {
                        Ok((
                            policy::parse(&args.vram_policy)?,
                            policy::parse(&args.host_policy)?,
                        ))
                    };
                    let host_compute = if args.host_threads(&tuning) > 0 {
                        args.host_compute(&tuning)
                    } else {
                        0
                    };
                    oominf_tiers::tiered_for_model(
                        b,
                        &files,
                        &budget,
                        lookahead,
                        host_compute,
                        &policies,
                        &io,
                    )?
                }
                other => anyhow::bail!("unknown --experts {other:?} (tiered, disk)"),
            })
        },
    )
}

/// What `open_model` loads and how.
pub struct OpenArgs<'a> {
    pub model_dir: &'a Path,
    /// Longest sequence a session is sized for by default.
    pub max_context: usize,
    /// Prompt tokens per prefill chunk (`None`: the model family's default).
    pub prefill_chunk: Option<usize>,
    pub experts: &'a ExpertArgs,
    pub cache: &'a CacheArgs,
    pub host_use: HostUse,
    /// Where `serve` gets its hardware profile (`None`: built-in defaults, as
    /// benchmarks and correctness tools want).
    pub profile: Option<&'a crate::profile::ProfileArgs>,
}

/// Loads a converted model on the CUDA device with the selected expert source.
#[cfg(not(target_os = "macos"))]
pub fn open_model(args: &OpenArgs) -> Result<Box<dyn Model>> {
    let files = Arc::new(Files::open(args.model_dir)?);
    let gpu = Arc::new(Gpu::new(0)?);
    let tuning = match args.profile {
        Some(p) => crate::profile::tuning(p, &gpu, &files, args.experts)?,
        None => Tuning::default(),
    };
    let opts = oominf_models::Options {
        max_context: args.max_context,
        prefill_chunk: args.prefill_chunk,
        host_threads: args.experts.host_threads(&tuning),
        kv: args.cache.kv_cache,
        dense: args.cache.dense,
        expert_precision: args.cache.expert_precision,
        attention_precision: args.cache.attention_precision,
        kv_host: args.cache.kv_host(),
    };
    let experts = factory::<Gpu>(args.experts, files.clone(), args.host_use, &tuning);
    oominf_models::open(gpu, files, &opts, experts)
}

#[cfg(target_os = "macos")]
pub fn validate_metal(args: &OpenArgs) -> Result<()> {
    anyhow::ensure!(
        args.cache.kv_cache == oominf_core::KvFormat::F32,
        "Metal currently uses exact fp32 KV state; pass --kv-cache fp32"
    );
    anyhow::ensure!(
        args.cache.dense == oominf_core::DenseFormat::Bf16
            && args.cache.expert_precision == oominf_core::ExpertPrecision::Exact
            && args.cache.attention_precision == oominf_core::AttentionPrecision::Exact,
        "Metal currently preserves released weights and uses exact fp32 activations"
    );
    anyhow::ensure!(
        !args.host_use.snapshots,
        "Metal prefix storage is not yet supported"
    );
    anyhow::ensure!(
        args.prefill_chunk.unwrap_or(1) == 1,
        "Metal currently prefills one token at a time"
    );
    if let Some(profile) = args.profile {
        anyhow::ensure!(
            profile.profile.is_none() && !profile.reprobe,
            "CUDA hardware profiles cannot configure Metal"
        );
    }
    anyhow::ensure!(
        matches!(args.experts.experts.as_str(), "tiered" | "disk"),
        "unknown expert source"
    );
    anyhow::ensure!(
        args.experts.host_compute.unwrap_or(0) == 0,
        "Metal computes routed experts on the GPU"
    );
    anyhow::ensure!(
        args.experts.host_expert_gib.is_none(),
        "Metal uses one shared expert cache; set --vram-expert-gib for its RAM budget"
    );
    Ok(())
}

#[cfg(target_os = "macos")]
pub fn open_model(args: &OpenArgs) -> Result<Box<dyn Model>> {
    validate_metal(args)?;
    let files = Arc::new(Files::open(args.model_dir)?);
    let mode = read_mode(args.experts, &files)?;
    let available = HostMemory::probe()?.usable();
    let default_reserve = if available >= 6 * (1 << 30) { 3. } else { 1. };
    let reserve = args.experts.host_reserve_gib.unwrap_or(default_reserve);
    eprintln!(
        "oominf: unified RAM reserve {reserve:.1} GiB; Metal and host buffers share one budget"
    );
    let cache = args.experts.vram_expert_gib.map(bytes);
    oominf_models_qwen35::open(
        files,
        oominf_models_qwen35::Options {
            max_context: args.max_context,
            reserve_bytes: bytes(reserve),
            cache_bytes: cache,
            io: oominf_tiers::host::IoConfig::new(mode),
            device_policy: oominf_tiers::policy::parse(&args.experts.vram_policy)?,
            host_policy: oominf_tiers::policy::parse(&args.experts.host_policy)?,
            disk_only: args.experts.experts == "disk",
        },
    )
}

/// Checksum of the files that identify a converted checkpoint (its index lists
/// every tensor's checksum).
pub fn checkpoint_id(model_dir: &Path) -> Result<String> {
    let mut bytes = std::fs::read(model_dir.join(oominf_format::INDEX_NAME))?;
    bytes.extend(std::fs::read(model_dir.join("config.json"))?);
    Ok(oominf_format::checksum(&bytes))
}
