//! `oominf bench`: greedy decode with expert-source statistics, the baseline every
//! performance change is measured against.
//!
//! Decode runs in two equal phases: the first starts from cold expert tiers (they
//! fill as it goes), the second measures the warmed steady state. The prompt then
//! runs again on a fresh sequence to measure prefill from warm tiers.

use std::path::Path;
use std::time::Instant;

use anyhow::{Context, Result};
use oominf_core::{ExpertStats, Model, Session};

use crate::chat::Chat;
use crate::generate::argmax;
use crate::load::{ExpertArgs, OpenArgs, open_model};

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

/// Where records came from, per `unit` (`"token"`) or in total.
fn tier_line(s: ExpertStats, tokens: usize, unit: &str) -> String {
    if s.requests == 0 {
        return String::new();
    }
    let per = |n: u64| n as f64 / tokens as f64;
    format!(
        "; {unit}: {:.1} records, {:.1} VRAM hits, {:.1} host hits, {:.1} disk reads (VRAM hit rate {:.1}%, host+VRAM {:.1}%)",
        per(s.requests),
        per(s.vram_hits),
        per(s.host_hits),
        per(s.disk_reads),
        100.0 * s.vram_hits as f64 / s.requests as f64,
        100.0 * (s.vram_hits + s.host_hits) as f64 / s.requests as f64
    ) + &prediction_line(&s)
}

fn decode_phase(
    name: &str,
    model: &dyn Model,
    session: &mut dyn Session,
    logits: &mut Vec<f32>,
    tokens: usize,
) -> Result<()> {
    let before = model.expert_stats();
    let t = Instant::now();
    for _ in 0..tokens {
        *logits = session.step(&[argmax(logits)])?;
    }
    let per = t.elapsed().as_secs_f64() / tokens as f64 * 1e3;
    println!(
        "{name}: {tokens} tokens, {per:.1} ms/token ({:.2} tok/s){}",
        1e3 / per,
        tier_line(model.expert_stats() - before, tokens, "per token")
    );
    Ok(())
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
    expert_args: &ExpertArgs,
) -> Result<()> {
    let chat = Chat::load(model_dir)?;
    let ids = chat.encode(&chat.render_user(prompt)?)?;
    let max_context = ids.len() + 2 * tokens + 1;
    let t = Instant::now();
    let model = open_model(&OpenArgs {
        model_dir,
        max_context,
        prefill_chunk,
        experts: expert_args,
    })?;
    println!(
        "{} (loaded in {:.1}s)",
        model.describe(),
        t.elapsed().as_secs_f64()
    );
    let mut session = model.new_session(max_context)?;
    let mut logits = timed_prefill("prefill", &*model, &mut *session, &ids)?;
    decode_phase(
        "decode (cold tiers)",
        &*model,
        &mut *session,
        &mut logits,
        tokens,
    )?;
    decode_phase(
        "decode (warm tiers)",
        &*model,
        &mut *session,
        &mut logits,
        tokens,
    )?;
    println!("after decode: {}", model.describe());
    // The same prompt again on a fresh sequence: prefill from warm tiers.
    drop(session);
    let mut session = model.new_session(ids.len() + 1)?;
    println!("fresh sequence: {}", model.describe());
    timed_prefill("prefill (warm tiers)", &*model, &mut *session, &ids)?;
    Ok(())
}
