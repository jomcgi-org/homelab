//! `oominf bench`: greedy decode with tier statistics and the host time spent in the
//! expert source, the baseline every performance change is measured against.
//!
//! Decode runs in two equal phases: the first starts from cold expert tiers (they
//! fill as it goes), the second measures the warmed steady state.

use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::Result;
use oominf_cuda::{Buf, Gpu};
use oominf_models_qwen::{Dims, ExpertSource, NoProbe, QwenModel, SeqState, Staged};
use oominf_tiers::TierStats;

use crate::chat::Chat;
use crate::experts::{ExpertArgs, Experts};

/// Wraps an expert source and accounts the host time spent inside it (waiting for
/// disk reads and enqueueing copies). Copies and kernels overlap on the device, so
/// the end-to-end ms/token is the number that matters.
struct Timed {
    inner: Experts,
    time: Duration,
    calls: usize,
}

impl ExpertSource for Timed {
    fn fetch(&mut self, gpu: &Gpu, layer: u32, experts: &[u32]) -> Result<Vec<u64>> {
        let t = Instant::now();
        let r = self.inner.fetch(gpu, layer, experts)?;
        self.time += t.elapsed();
        self.calls += experts.len();
        Ok(r)
    }

    fn begin_fetch(&mut self, gpu: &Gpu, layer: u32, experts: &[u32]) -> Result<Staged> {
        let t = Instant::now();
        let r = self.inner.begin_fetch(gpu, layer, experts)?;
        self.time += t.elapsed();
        self.calls += experts.len();
        Ok(r)
    }

    fn finish_fetch(&mut self, gpu: &Gpu) -> Result<()> {
        let t = Instant::now();
        self.inner.finish_fetch(gpu)?;
        self.time += t.elapsed();
        Ok(())
    }
}

fn tier_line(s: Option<TierStats>, tokens: usize) -> String {
    match s {
        Some(s) if s.requests > 0 => {
            let per = |n: u64| n as f64 / tokens as f64;
            format!(
                "; per token: {:.1} VRAM hits, {:.1} host hits, {:.1} disk reads (VRAM hit rate {:.1}%, host+VRAM {:.1}%)",
                per(s.vram_hits),
                per(s.host_hits),
                per(s.disk_reads),
                100.0 * s.vram_hits as f64 / s.requests as f64,
                100.0 * (s.vram_hits + s.host_hits) as f64 / s.requests as f64
            )
        }
        _ => String::new(),
    }
}

#[allow(clippy::too_many_arguments)]
fn decode_phase(
    name: &str,
    gpu: &Gpu,
    qwen: &QwenModel,
    state: &mut SeqState,
    experts: &mut Timed,
    logits: &mut Buf,
    tokens: usize,
) -> Result<()> {
    experts.time = Duration::ZERO;
    experts.calls = 0;
    let before = experts.inner.stats();
    let t = Instant::now();
    for _ in 0..tokens {
        let row = gpu.download(logits)?;
        let next = row
            .iter()
            .enumerate()
            .max_by(|a, b| a.1.total_cmp(b.1))
            .map(|(i, _)| i as u32)
            .unwrap();
        *logits = qwen.forward(gpu, &[next], state, experts, &mut NoProbe, true)?;
    }
    gpu.sync()?;
    let total = t.elapsed().as_secs_f64();
    let per = total / tokens as f64 * 1e3;
    let load = experts.time.as_secs_f64() / tokens as f64 * 1e3;
    let stats = match (experts.inner.stats(), before) {
        (Some(a), Some(b)) => Some(a - b),
        _ => None,
    };
    println!(
        "{name}: {tokens} tokens, {per:.1} ms/token ({:.2} tok/s); host time in expert source {load:.1} ms/token ({} records/token){}",
        1e3 / per,
        experts.calls / tokens,
        tier_line(stats, tokens)
    );
    Ok(())
}

/// Prefills `ids` layer by layer in `chunk`-token slices; last row of logits.
fn prefill(
    gpu: &Gpu,
    qwen: &QwenModel,
    ids: &[u32],
    chunk: usize,
    state: &mut SeqState,
    experts: &mut dyn ExpertSource,
) -> Result<Buf> {
    qwen.prefill(gpu, ids, chunk, state, experts, &|| false)?
        .ok_or_else(|| anyhow::anyhow!("prefill cancelled"))
}

pub fn run(
    model_dir: &Path,
    prompt: &str,
    tokens: usize,
    prefill_chunk: usize,
    expert_args: &ExpertArgs,
) -> Result<()> {
    let chat = Chat::load(model_dir)?;
    let ids = chat.encode(&chat.render_user(prompt)?)?;
    let model = Arc::new(oominf_format::Model::open(model_dir)?);
    let dims = Dims::from_config(&std::fs::read_to_string(model_dir.join("config.json"))?)?;
    let gpu = Gpu::new(0)?;
    let qwen = QwenModel::load(&gpu, &model, dims, None)?;
    // Sequence state first, so automatic tier sizing sees the VRAM that is left.
    let mut state = qwen.new_state(&gpu, ids.len() + 2 * tokens + 1)?;
    let t = Instant::now();
    let inner = Experts::build(expert_args, &gpu, &model)?;
    println!(
        "{} (set up in {:.1}s)",
        inner.describe(),
        t.elapsed().as_secs_f64()
    );
    let mut experts = Timed {
        inner,
        time: Duration::ZERO,
        calls: 0,
    };
    let before = experts.inner.stats();
    let t = Instant::now();
    let mut logits = prefill(&gpu, &qwen, &ids, prefill_chunk, &mut state, &mut experts)?;
    gpu.sync()?;
    let stats = match (experts.inner.stats(), before) {
        (Some(a), Some(b)) => Some(a - b),
        _ => None,
    };
    println!(
        "prefill {} tokens: {:.2}s (host time in expert source {:.2}s over {} records){}",
        ids.len(),
        t.elapsed().as_secs_f64(),
        experts.time.as_secs_f64(),
        experts.calls,
        tier_line(stats, 1).replace("per token", "in total")
    );
    decode_phase(
        "decode (cold tiers)",
        &gpu,
        &qwen,
        &mut state,
        &mut experts,
        &mut logits,
        tokens,
    )?;
    decode_phase(
        "decode (warm tiers)",
        &gpu,
        &qwen,
        &mut state,
        &mut experts,
        &mut logits,
        tokens,
    )?;
    // The same prompt again on a fresh sequence: prefill from warm tiers.
    drop(state);
    let mut state = qwen.new_state(&gpu, ids.len() + 1)?;
    let before = experts.inner.stats();
    let t = Instant::now();
    prefill(&gpu, &qwen, &ids, prefill_chunk, &mut state, &mut experts)?;
    gpu.sync()?;
    let stats = match (experts.inner.stats(), before) {
        (Some(a), Some(b)) => Some(a - b),
        _ => None,
    };
    println!(
        "prefill {} tokens (warm tiers): {:.2}s{}",
        ids.len(),
        t.elapsed().as_secs_f64(),
        tier_line(stats, 1).replace("per token", "in total")
    );
    Ok(())
}
