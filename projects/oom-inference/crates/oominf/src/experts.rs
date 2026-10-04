//! Expert-source selection shared by the commands that run the model.

use std::sync::Arc;

use anyhow::Result;
use oominf_cuda::Gpu;
use oominf_format::Model;
use oominf_models_qwen::{DiskExperts, ExpertSource, Staged};
use oominf_tiers::{TierStats, TieredExperts, policy};

#[derive(clap::Args, Debug, Clone)]
pub struct ExpertArgs {
    /// Where routed experts come from: `tiered` (VRAM, pinned host, disk) or `disk`
    /// (read and upload every record, no cache).
    #[arg(long, default_value = "tiered")]
    pub experts: String,
    /// VRAM budget for cached expert records (default: free VRAM when the tiers are
    /// built, minus `--vram-reserve-gib`; build them after allocating sequence state).
    #[arg(long)]
    pub vram_expert_gib: Option<f64>,
    /// VRAM left free for activations and later allocations when sizing automatically.
    #[arg(long, default_value_t = 2.0)]
    pub vram_reserve_gib: f64,
    /// Pinned host-memory budget for cached expert records (default: available RAM
    /// minus `--host-reserve-gib`).
    #[arg(long)]
    pub host_expert_gib: Option<f64>,
    /// RAM left available (page cache for PLE rows, the OS, other processes) when
    /// sizing automatically.
    #[arg(long, default_value_t = 12.0)]
    pub host_reserve_gib: f64,
    /// VRAM tier policy: `lru`, `lfu` or `lrfu:<half-life in expert accesses>`.
    #[arg(long, default_value = "lrfu:7680")]
    pub vram_policy: String,
    /// Host tier policy (same syntax).
    #[arg(long, default_value = "lrfu:30720")]
    pub host_policy: String,
}

pub enum Experts {
    Disk(DiskExperts),
    Tiered(Box<TieredExperts>),
}

impl Experts {
    pub fn build(args: &ExpertArgs, gpu: &Gpu, model: &Arc<Model>) -> Result<Self> {
        Ok(match args.experts.as_str() {
            "disk" => Experts::Disk(DiskExperts::new(model.clone())),
            "tiered" => {
                let vram = match args.vram_expert_gib {
                    Some(g) => g,
                    None => TieredExperts::free_vram_gib(gpu, args.vram_reserve_gib)?,
                };
                let host = match args.host_expert_gib {
                    Some(g) => g,
                    None => TieredExperts::available_host_gib(args.host_reserve_gib)?,
                };
                Experts::Tiered(Box::new(TieredExperts::new(
                    gpu,
                    model.clone(),
                    TieredExperts::slots_for(model, vram),
                    TieredExperts::slots_for(model, host),
                    policy::parse(&args.vram_policy)?,
                    policy::parse(&args.host_policy)?,
                )?))
            }
            other => anyhow::bail!("unknown --experts {other:?} (tiered, disk)"),
        })
    }

    pub fn describe(&self) -> String {
        match self {
            Experts::Disk(_) => "disk experts (no cache)".into(),
            Experts::Tiered(t) => t.describe(),
        }
    }

    pub fn stats(&self) -> Option<TierStats> {
        match self {
            Experts::Disk(_) => None,
            Experts::Tiered(t) => Some(t.stats),
        }
    }
}

impl ExpertSource for Experts {
    fn fetch(&mut self, gpu: &Gpu, layer: u32, experts: &[u32]) -> Result<Vec<u64>> {
        match self {
            Experts::Disk(d) => d.fetch(gpu, layer, experts),
            Experts::Tiered(t) => t.fetch(gpu, layer, experts),
        }
    }

    fn begin_fetch(&mut self, gpu: &Gpu, layer: u32, experts: &[u32]) -> Result<Staged> {
        match self {
            Experts::Disk(d) => d.begin_fetch(gpu, layer, experts),
            Experts::Tiered(t) => t.begin_fetch(gpu, layer, experts),
        }
    }

    fn finish_fetch(&mut self, gpu: &Gpu) -> Result<()> {
        match self {
            Experts::Disk(d) => d.finish_fetch(gpu),
            Experts::Tiered(t) => t.finish_fetch(gpu),
        }
    }
}
