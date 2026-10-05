//! Opening a model for the commands that run it: the CUDA backend, the model
//! registry and the expert source chosen on the command line.

use std::path::Path;
use std::sync::Arc;

use anyhow::Result;
use oominf_core::{Backend, ExpertFactory, ExpertSource, Model};
use oominf_cuda::Gpu;
use oominf_format::Model as Files;
use oominf_tiers::{DiskExperts, policy};

#[derive(clap::Args, Debug, Clone)]
pub struct ExpertArgs {
    /// Where routed experts come from: `tiered` (VRAM, pinned host, disk) or `disk`
    /// (read and upload every record, no cache).
    #[arg(long, default_value = "tiered")]
    pub experts: String,
    /// VRAM budget for cached expert records (default: free VRAM once the model and
    /// a first sequence are loaded, minus `--vram-reserve-gib`).
    #[arg(long)]
    pub vram_expert_gib: Option<f64>,
    /// VRAM left free for activations and later allocations when sizing automatically.
    #[arg(long, default_value_t = 2.0)]
    pub vram_reserve_gib: f64,
    /// Pinned host-memory budget for cached expert records (default: available RAM
    /// minus `--host-reserve-gib`).
    #[arg(long)]
    pub host_expert_gib: Option<f64>,
    /// RAM left available (page cache for embedding-table rows, the OS, other
    /// processes) when sizing automatically.
    #[arg(long, default_value_t = 12.0)]
    pub host_reserve_gib: f64,
    /// VRAM tier policy: `lru`, `lfu` or `lrfu:<half-life in expert accesses>`.
    #[arg(long, default_value = "lrfu:7680")]
    pub vram_policy: String,
    /// Host tier policy (same syntax).
    #[arg(long, default_value = "lrfu:30720")]
    pub host_policy: String,
    /// During decode, predict the next layer's experts and start reading predicted
    /// disk misses into the host tier: `on` or `off`.
    #[arg(long, default_value = "on")]
    pub lookahead: String,
    /// Most routed experts per layer and decode step computed on the CPU from host
    /// memory instead of copied to the device (0 copies every one).
    #[arg(long, default_value_t = 6)]
    pub host_compute: usize,
    /// CPU threads for those experts (default: one per physical core).
    #[arg(long)]
    pub host_threads: Option<usize>,
}

/// Runtime choices for a model's sequences, beyond where experts come from.
#[derive(clap::Args, Debug, Clone)]
pub struct CacheArgs {
    /// How attention layers store their KV cache: compressed TurboQuant-style as
    /// `k<bits>v<bits>` or `tq<bits>` (2 to 8 bits per coordinate), or `fp32` (exact).
    /// The default `k8v6` measured inside fp32's own rounding noise at 32k-95k tokens
    /// while keeping about 3x less KV memory (#6830); other modes are lossy to
    /// different degrees: judge them with `oominf score`.
    #[arg(long, default_value = "k8v6", value_parser = oominf_core::KvFormat::parse)]
    pub kv_cache: oominf_core::KvFormat,
    /// How dense (non-expert) weights are stored: `bf16` as in the checkpoint
    /// (exact), or `fp8` (e4m3 with a scale per 128 weights: half the bytes, so faster
    /// decode and more VRAM for experts; lossy, judge with `oominf score`).
    #[arg(long, default_value = "bf16", value_parser = oominf_core::DenseFormat::parse)]
    pub dense: oominf_core::DenseFormat,
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

impl ExpertArgs {
    /// CPU threads computing host-resident experts (0 when host compute is off).
    pub fn host_threads(&self) -> usize {
        if self.host_compute == 0 {
            return 0;
        }
        self.host_threads.unwrap_or_else(|| {
            std::thread::available_parallelism().map_or(4, |n| (n.get() / 2).max(1))
        })
    }
}

/// Builds the expert source `args` selects, once the model is on the device.
pub fn factory<B: Backend>(args: &ExpertArgs, files: Arc<Files>) -> ExpertFactory<B> {
    let args = args.clone();
    Box::new(move |b: &Arc<B>| -> Result<Box<dyn ExpertSource<B>>> {
        Ok(match args.experts.as_str() {
            "disk" => Box::new(DiskExperts::new(files)),
            "tiered" => {
                let vram = match args.vram_expert_gib {
                    Some(g) => g,
                    None => oominf_tiers::free_vram_gib(&**b, args.vram_reserve_gib)?,
                };
                let host = match args.host_expert_gib {
                    Some(g) => g,
                    None => oominf_tiers::available_host_gib(args.host_reserve_gib)?,
                };
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
                let host_compute = if args.host_threads() > 0 {
                    args.host_compute
                } else {
                    0
                };
                oominf_tiers::tiered_for_model(
                    b,
                    &files,
                    vram,
                    host,
                    lookahead,
                    host_compute,
                    &policies,
                )?
            }
            other => anyhow::bail!("unknown --experts {other:?} (tiered, disk)"),
        })
    })
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
}

/// Loads a converted model on the CUDA device with the selected expert source.
pub fn open_model(args: &OpenArgs) -> Result<Box<dyn Model>> {
    let files = Arc::new(Files::open(args.model_dir)?);
    let gpu = Arc::new(Gpu::new(0)?);
    let opts = oominf_models::Options {
        max_context: args.max_context,
        prefill_chunk: args.prefill_chunk,
        host_threads: args.experts.host_threads(),
        kv: args.cache.kv_cache,
        dense: args.cache.dense,
        kv_host: args.cache.kv_host(),
    };
    let experts = factory::<Gpu>(args.experts, files.clone());
    oominf_models::open(gpu, files, &opts, experts)
}

/// Checksum of the files that identify a converted checkpoint (its index lists
/// every tensor's checksum).
pub fn checkpoint_id(model_dir: &Path) -> Result<String> {
    let mut bytes = std::fs::read(model_dir.join(oominf_format::INDEX_NAME))?;
    bytes.extend(std::fs::read(model_dir.join("config.json"))?);
    Ok(oominf_format::checksum(&bytes))
}
