//! `oominf doctor`: what `serve` would decide on this machine, without starting
//! it: the hardware fingerprint, the cached profile, the fresh safety checks and
//! the effective configuration with its warnings. It does not measure (`oominf
//! tune` does) and does not load the model, so VRAM after loading is estimated.

use std::path::Path;

use anyhow::Result;
use oominf_core::{DenseFormat, Memory};
use oominf_cuda::Gpu;
use oominf_format::Model as Files;

use crate::load::{CacheArgs, ExpertArgs, HostUse, Tuning};
use crate::profile::{self, Fingerprint, Profile, ProfileArgs};

const GIB: f64 = (1u64 << 30) as f64;

/// Device memory the loaded model and its first sequence take beyond the dense
/// weights' file size scaled for `--dense`: an estimate (the first sequence's
/// state, workspaces and the CUDA context), not a measurement.
const LOAD_OVERHEAD: u64 = 1 << 30;

/// Host memory the loaded model holds when the tiers are sized (tokenizer,
/// host-side tables and buffers): measured 1.5 GiB in the container runs of
/// `docs/HARDWARE.md`.
const HOST_LOAD_OVERHEAD: u64 = 3 << 29;

pub struct Settings<'a> {
    pub max_context: usize,
    pub max_streams: usize,
    pub prefix_store: bool,
    pub experts: &'a ExpertArgs,
    pub cache: &'a CacheArgs,
    pub probe: &'a ProfileArgs,
}

/// Prints the report; fails when `serve` would fail to start.
pub fn run(model_dir: &Path, s: &Settings) -> Result<()> {
    let files = Files::open(model_dir)?;
    let gpu = Gpu::new(0)?;
    let mode = crate::load::read_mode(s.experts, &files)?;
    let fp = Fingerprint::collect(&gpu, &files, mode)?;
    println!("fingerprint:");
    println!(
        "  gpu {} ({:.1} GiB), driver {}",
        fp.gpu,
        fp.vram_bytes as f64 / GIB,
        fp.driver
    );
    println!(
        "  cpu {} ({} usable CPUs), RAM {:.1} GiB",
        fp.cpu,
        fp.cpus,
        fp.ram_bytes as f64 / GIB
    );
    println!("  model storage {}, read path {}", fp.storage, fp.read_path);
    println!("  engine {}", fp.engine);

    let mut tuning = Tuning::default();
    let cached = match (&s.probe.profile, profile::cache_dir()) {
        _ if s.probe.no_probe => {
            println!("profile: skipped (--no-probe): built-in defaults");
            None
        }
        (Some(p), _) => Some((p.clone(), Profile::load(p)?)),
        (None, Some(dir)) => {
            let path = dir.join(fp.file_name());
            match Profile::load(&path) {
                Ok(p) if p.fingerprint == fp => Some((path, p)),
                _ => {
                    println!(
                        "profile: none cached for this fingerprint at {}; serve measures on start (a few seconds), or run oominf tune",
                        path.display()
                    );
                    None
                }
            }
        }
        (None, None) => {
            println!("profile: no cache directory (HOME unset)");
            None
        }
    };
    if let Some((path, p)) = cached {
        println!("profile file {}:", path.display());
        profile::report(&p, &files)?;
        tuning = profile::derive(&p, &files)?.0;
    }

    println!("fresh checks:");
    println!("  expert reads: {}", mode.describe());
    let (free, total) = gpu.mem_info()?;
    println!(
        "  GPU memory: {:.1} GiB free of {:.1} GiB now",
        free as f64 / GIB,
        total as f64 / GIB
    );
    let dense = files.index().files.dense.bytes as f64;
    let weights = match s.cache.dense {
        DenseFormat::Bf16 => dense,
        // One byte per weight plus a 4-byte scale per 128.
        DenseFormat::Fp8 => dense * (0.5 + 4.0 / 256.0),
    } as u64;
    let after = (free as u64).saturating_sub(weights + LOAD_OVERHEAD);
    println!(
        "  GPU memory after loading (estimate): {:.1} GiB ({:.1} GiB of dense weights, {:.1} GiB for the first sequence and context)",
        after as f64 / GIB,
        weights as f64 / GIB,
        LOAD_OVERHEAD as f64 / GIB
    );

    println!("effective configuration:");
    let hc = s.experts.host_compute(&tuning);
    let source = |flag: bool, profile: bool| {
        if flag {
            "flag"
        } else if profile {
            "profile"
        } else {
            "default"
        }
    };
    println!(
        "  host compute {hc} experts per layer on {} threads ({})",
        s.experts.host_threads(&tuning),
        source(
            s.experts.host_compute.is_some(),
            tuning.host_compute.is_some()
        )
    );
    if let Some(r) = tuning.host_stage_slots {
        println!("  prefill staging ring {r} records (profile)");
    }
    let opts = oominf_models::Options {
        max_context: s.max_context,
        prefill_chunk: None,
        host_threads: 0,
        kv: s.cache.kv_cache,
        dense: s.cache.dense,
        expert_precision: s.cache.expert_precision,
        attention_precision: s.cache.attention_precision,
        kv_host: s.cache.kv_host(),
    };
    let demand = oominf_models::host_demand(&files, &opts)?;
    let host_use = HostUse {
        sequences: s.max_streams + 1,
        checkpoints: oominf_server::engine::max_checkpoints(s.max_context),
        snapshots: s.prefix_store,
    };
    println!(
        "  host memory the loaded model holds (estimate): {:.1} GiB",
        HOST_LOAD_OVERHEAD as f64 / GIB
    );
    match crate::load::tier_budget(
        s.experts,
        &files,
        after,
        HOST_LOAD_OVERHEAD,
        &demand,
        &host_use,
        &tuning,
    ) {
        Ok(r) => {
            for l in &r.lines {
                println!("  {l}");
            }
            for w in &r.warnings {
                println!("  warning: {w}");
            }
            println!("serve would start");
            Ok(())
        }
        Err(e) => {
            println!("serve would not start: {e:#}");
            Err(e.context("not enough memory for the smallest working configuration"))
        }
    }
}
