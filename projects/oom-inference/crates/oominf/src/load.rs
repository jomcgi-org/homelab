//! Opening a model for the commands that run it: the CUDA backend, the model
//! registry and the expert source chosen on the command line.

use std::path::Path;
use std::sync::Arc;

use anyhow::Result;
use oominf_core::{Backend, ExpertFactory, ExpertSource, Model};
use oominf_cuda::Gpu;
use oominf_format::Model as Files;
use oominf_tiers::{DiskExperts, TieredExperts, policy};

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
                let mut tiered = TieredExperts::new(
                    b.clone(),
                    files.clone(),
                    oominf_tiers::slots_for(&files, vram),
                    oominf_tiers::slots_for(&files, host),
                    policy::parse(&args.vram_policy)?,
                    policy::parse(&args.host_policy)?,
                )?;
                tiered.lookahead = lookahead;
                Box::new(tiered)
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
}

/// Loads a converted model on the CUDA device with the selected expert source.
pub fn open_model(args: &OpenArgs) -> Result<Box<dyn Model>> {
    let files = Arc::new(Files::open(args.model_dir)?);
    let gpu = Arc::new(Gpu::new(0)?);
    let opts = oominf_models::Options {
        max_context: args.max_context,
        prefill_chunk: args.prefill_chunk,
    };
    let experts = factory::<Gpu>(args.experts, files.clone());
    oominf_models::open(gpu, files, &opts, experts)
}
