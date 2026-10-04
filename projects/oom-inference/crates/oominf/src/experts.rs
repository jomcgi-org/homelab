//! Expert-source selection shared by the commands that run the model.

use std::sync::Arc;

use anyhow::Result;
use oominf_cuda::Gpu;
use oominf_format::Model;
use oominf_models_qwen::{DiskExperts, ExpertSource};
use oominf_tiers::{TierStats, TieredExperts, policy};

#[derive(clap::Args, Debug, Clone)]
pub struct ExpertArgs {
    /// Where routed experts come from: `tiered` (VRAM, pinned host, disk) or `disk`
    /// (read and upload every record, no cache).
    #[arg(long, default_value = "tiered")]
    pub experts: String,
    /// VRAM budget for cached expert records.
    #[arg(long, default_value_t = 6.0)]
    pub vram_expert_gib: f64,
    /// Pinned host-memory budget for cached expert records.
    #[arg(long, default_value_t = 24.0)]
    pub host_expert_gib: f64,
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
            "tiered" => Experts::Tiered(Box::new(TieredExperts::new(
                gpu,
                model.clone(),
                TieredExperts::slots_for(model, args.vram_expert_gib),
                TieredExperts::slots_for(model, args.host_expert_gib),
                policy::parse(&args.vram_policy)?,
                policy::parse(&args.host_policy)?,
            )?)),
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
}
