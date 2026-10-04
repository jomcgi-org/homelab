//! `oominf bench`: greedy decode (speculative when the model drafts) with
//! expert-source statistics, the baseline every performance change is measured
//! against.
//!
//! Decode runs in two equal phases: the first starts from cold expert tiers (they
//! fill as it goes), the second measures the warmed steady state. The prompt then
//! runs again on a fresh sequence to measure prefill from warm tiers.

use std::path::Path;
use std::time::Instant;

use anyhow::{Context, Result};
use oominf_core::{ExpertStats, Model, Session, decode_step};

use crate::chat::Chat;
use crate::load::{CacheArgs, ExpertArgs, OpenArgs, open_model};
use oominf_core::argmax;

/// Prediction precision and recall, and how many lookahead reads a fetch used.
fn prediction_line(s: &ExpertStats) -> String {
    if s.predicted == 0 {
        return String::new();
    }
    let pct = |a: u64, b: u64| 100.0 * a as f64 / b.max(1) as f64;
    format!(
        "; prediction precision {:.1}%, recall {:.1}%; lookahead reads {} ({:.1}% used)",
        pct(s.predicted_routed, s.predicted),
        pct(s.predicted_routed, s.routed_after_prediction),
        s.lookahead_reads,
        pct(s.lookahead_used, s.lookahead_reads)
    )
}

/// Records staged ahead of their layer's fetch, and how many it used.
fn staged_line(s: &ExpertStats) -> String {
    if s.staged == 0 {
        return String::new();
    }
    format!(
        "; staged ahead {} ({:.1}% used)",
        s.staged,
        100.0 * s.staged_used as f64 / s.staged as f64
    )
}

/// Where records came from, per `unit` (`"token"`) or in total.
fn tier_line(s: ExpertStats, tokens: usize, unit: &str) -> String {
    if s.requests == 0 {
        return String::new();
    }
    let per = |n: u64| n as f64 / tokens as f64;
    format!(
        "; {unit}: {:.1} records, {:.1} VRAM hits, {:.1} host hits ({:.1} computed on the CPU), {:.1} disk reads (VRAM hit rate {:.1}%, host+VRAM {:.1}%)",
        per(s.requests),
        per(s.vram_hits),
        per(s.host_hits),
        per(s.host_computed),
        per(s.disk_reads),
        100.0 * s.vram_hits as f64 / s.requests as f64,
        100.0 * (s.vram_hits + s.host_hits) as f64 / s.requests as f64
    ) + &prediction_line(&s)
        + &staged_line(&s)
}

/// Generates at least `tokens` tokens greedily from `next` (the last generated,
/// not yet fed token), drafting up to `draft` tokens per step; returns them.
fn decode_phase(
    name: &str,
    model: &dyn Model,
    session: &mut dyn Session,
    next: &mut u32,
    tokens: usize,
    draft: usize,
) -> Result<Vec<u32>> {
    let before = model.expert_stats();
    let t = Instant::now();
    let (mut out, mut steps, mut drafted, mut accepted) = (Vec::new(), 0, 0, 0);
    while out.len() < tokens {
        let d = decode_step(session, *next, draft, |row: &[f32], _| Ok(argmax(row)))?;
        steps += 1;
        drafted += d.drafted;
        accepted += d.accepted;
        *next = *d.tokens.last().expect("a step produces a token");
        out.extend(d.tokens);
    }
    let n = out.len();
    let per = t.elapsed().as_secs_f64() / n as f64 * 1e3;
    let spec = if drafted > 0 {
        format!(
            "; {:.2} tokens/step, drafts accepted {accepted}/{drafted} ({:.1}%)",
            n as f64 / steps as f64,
            100.0 * accepted as f64 / drafted as f64
        )
    } else {
        String::new()
    };
    println!(
        "{name}: {n} tokens, {per:.1} ms/token ({:.2} tok/s){spec}{}",
        1e3 / per,
        tier_line(model.expert_stats() - before, n, "per token")
    );
    Ok(out)
}

fn timed_prefill(
    name: &str,
    model: &dyn Model,
    session: &mut dyn Session,
    ids: &[u32],
) -> Result<Vec<f32>> {
    let before = model.expert_stats();
    let t = Instant::now();
    let logits = session
        .prefill(ids, &|| false)?
        .context("prefill cancelled")?;
    println!(
        "{name} {} tokens: {:.2}s{}",
        ids.len(),
        t.elapsed().as_secs_f64(),
        tier_line(model.expert_stats() - before, 1, "in total")
    );
    Ok(logits)
}

pub fn run(
    model_dir: &Path,
    prompt: &str,
    tokens: usize,
    prefill_chunk: Option<usize>,
    draft: usize,
    expert_args: &ExpertArgs,
    cache: &CacheArgs,
) -> Result<()> {
    let chat = Chat::load(model_dir)?;
    let ids = chat.encode(&chat.render_user(prompt)?)?;
    let max_context = ids.len() + 2 * (tokens + draft) + 1;
    let t = Instant::now();
    let model = open_model(&OpenArgs {
        model_dir,
        max_context,
        prefill_chunk,
        experts: expert_args,
        cache,
    })?;
    println!(
        "{} (loaded in {:.1}s)",
        model.describe(),
        t.elapsed().as_secs_f64()
    );
    let mut session = model.new_session(max_context)?;
    let logits = timed_prefill("prefill", &*model, &mut *session, &ids)?;
    let mut next = argmax(&logits);
    decode_phase(
        "decode (cold tiers)",
        &*model,
        &mut *session,
        &mut next,
        tokens,
        draft,
    )?;
    decode_phase(
        "decode (warm tiers)",
        &*model,
        &mut *session,
        &mut next,
        tokens,
        draft,
    )?;
    println!("after decode: {}", model.describe());
    // The same prompt again on a fresh sequence: prefill from warm tiers.
    drop(session);
    let mut session = model.new_session(ids.len() + 1)?;
    println!("fresh sequence: {}", model.describe());
    timed_prefill("prefill (warm tiers)", &*model, &mut *session, &ids)?;
    Ok(())
}
