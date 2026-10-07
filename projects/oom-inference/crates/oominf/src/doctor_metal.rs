//! Shared-memory dry run for the Mac decoder; no model weights are loaded.
use std::path::Path;

use anyhow::{Result, ensure};
use oominf_core::Memory;
use oominf_format::Model as Files;
use oominf_metal::Gpu;
use oominf_tiers::resources::HostMemory;

use crate::load::{CacheArgs, ExpertArgs, HostUse, OpenArgs};
use crate::profile::ProfileArgs;

const GIB: f64 = (1u64 << 30) as f64;

pub struct Settings<'a> {
    pub max_context: usize,
    pub max_streams: usize,
    pub prefix_store: bool,
    pub experts: &'a ExpertArgs,
    pub cache: &'a CacheArgs,
    pub probe: &'a ProfileArgs,
}

pub fn run(model_dir: &Path, s: &Settings) -> Result<()> {
    ensure!(s.max_streams > 0, "max streams must be positive");
    crate::load::validate_metal(&OpenArgs {
        model_dir,
        max_context: s.max_context,
        prefill_chunk: None,
        experts: s.experts,
        cache: s.cache,
        host_use: HostUse {
            sequences: s.max_streams + 1,
            checkpoints: 0,
            snapshots: s.prefix_store,
        },
        profile: Some(s.probe),
    })?;
    let files = Files::open(model_dir)?;
    let mode = crate::load::read_mode(s.experts, &files)?;
    let estimate = oominf_models_qwen35::memory_estimate(&files, s.max_context)?;
    let host = HostMemory::probe()?;
    let reserve = s
        .experts
        .host_reserve_gib
        .unwrap_or(if host.usable() >= 6 * (1 << 30) {
            3.
        } else {
            1.
        });
    let gpu = Gpu::new()?;
    let limit = host
        .usable()
        .saturating_sub((reserve * GIB) as u64)
        .min(gpu.mem_info()?.1 as u64);
    let held = estimate.resident_weights
        + estimate.sequence * (s.max_streams + 1)
        + estimate.staging
        + (128 << 20);
    let available = (limit as usize).saturating_sub(held);
    let cache = s
        .experts
        .vram_expert_gib
        .map_or(available, |n| available.min((n * GIB) as usize));
    println!(
        "GPU: {}; shared memory, no separate VRAM budget",
        gpu.name()
    );
    println!("RAM: {}; reserve {reserve:.1} GiB", host.describe());
    println!("Expert reads: {}", mode.describe());
    println!("Shared limit: {:.2} GiB", limit as f64 / GIB);
    println!(
        "Estimated resident weights: {:.2} GiB (embedding rows paged)",
        estimate.resident_weights as f64 / GIB
    );
    println!(
        "Sequence state: {:.2} GiB each, {} active plus one cached",
        estimate.sequence as f64 / GIB,
        s.max_streams
    );
    println!(
        "Expert cache allowance: {:.2} GiB; exact fp32 KV and activations",
        cache as f64 / GIB
    );
    println!("CUDA tuning profiles are not used on Metal; these are estimates, not a load test.");
    ensure!(
        limit >= 2 * (1 << 30) && held <= limit as usize,
        "not enough shared RAM for weights and sequence state"
    );
    ensure!(
        s.experts.experts == "disk" || cache >= estimate.record_stride * oominf_tiers::CHUNK_SLOTS,
        "not enough shared RAM for the minimum expert cache"
    );
    println!("serve fits the estimated budget; fresh allocation checks run when loading");
    Ok(())
}
