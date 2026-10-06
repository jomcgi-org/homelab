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
use oominf_core::{ExpertStats, LOOKUP_MATCH, Model, PromptLookup, Session, decode_step_with};

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
/// Prompt-lookup state for [`decode_phase`]: the drafter and every token so far
/// (prompt and output, not counting the unfed `next`).
struct LookupRun {
    lookup: PromptLookup,
    history: Vec<u32>,
}

#[allow(clippy::too_many_arguments)]
fn decode_phase(
    name: &str,
    model: &dyn Model,
    session: &mut dyn Session,
    next: &mut u32,
    tokens: usize,
    draft: usize,
    mut lookup: Option<&mut LookupRun>,
) -> Result<Vec<u32>> {
    let before = model.expert_stats();
    let t = Instant::now();
    let (mut out, mut steps, mut drafted, mut accepted) = (Vec::new(), 0, 0, 0);
    // Per verification width (tokens fed): steps, time and tokens kept.
    let mut widths: std::collections::BTreeMap<usize, (usize, f64, usize)> = Default::default();
    while out.len() < tokens {
        let drafts = lookup.as_deref_mut().map(|l| {
            l.history.push(*next);
            let d = l.lookup.draft(&l.history);
            l.history.pop();
            d
        });
        let s = Instant::now();
        let d = decode_step_with(session, *next, draft, drafts, |row: &[f32], _| {
            Ok(argmax(row))
        })?;
        let e = widths.entry(d.drafted + 1).or_default();
        e.0 += 1;
        e.1 += s.elapsed().as_secs_f64() * 1e3;
        e.2 += d.tokens.len();
        steps += 1;
        drafted += d.drafted;
        accepted += d.accepted;
        if let Some(l) = lookup.as_deref_mut() {
            l.history.push(*next);
            l.history.extend(&d.tokens[..d.tokens.len() - 1]);
        }
        *next = *d.tokens.last().expect("a step produces a token");
        out.extend(d.tokens);
    }
    if lookup.is_some() {
        for (w, (n, ms, kept)) in &widths {
            println!(
                "  width {w:2}: {n:4} steps, {:6.1} ms/step, {:4.2} tokens kept/step",
                ms / *n as f64,
                *kept as f64 / *n as f64
            );
        }
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

/// Times verification steps of each width in `widths` (tokens fed at once, the
/// last generated token plus `width - 1` drafts): the cost of checking a
/// prompt-lookup draft, whose tokens copy a span of the prompt. Each step keeps
/// only its first token (every draft rejected, as a failed draft would) and
/// rewinds the rest, so the sequence advances one token per step.
fn verify_sweep(
    model: &dyn Model,
    session: &mut dyn Session,
    next: &mut u32,
    ids: &[u32],
    widths: &[usize],
) -> Result<()> {
    const STEPS: usize = 16;
    for &width in widths {
        let width = width.max(1);
        let span = width - 1;
        let room = ids.len().saturating_sub(span).max(1);
        let before = model.expert_stats();
        let t = Instant::now();
        for step in 0..STEPS {
            let at = (step * 7919 + width * 131) % room;
            let mut feed = vec![*next];
            feed.extend(ids.iter().skip(at).take(span));
            let logits = session.step_all(&feed)?;
            let vocab = logits.len() / feed.len();
            session.rewind(feed.len() - 1)?;
            *next = argmax(&logits[..vocab]);
        }
        let per = t.elapsed().as_secs_f64() / STEPS as f64 * 1e3;
        println!(
            "verify {width:2} tokens: {per:.1} ms/step ({:.1} ms per token if all accepted){}",
            per / width as f64,
            tier_line(model.expert_stats() - before, STEPS, "per step")
        );
    }
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

#[allow(clippy::too_many_arguments)]
pub fn run(
    model_dir: &Path,
    prompt: &str,
    tokens: usize,
    prefill_chunk: Option<usize>,
    draft: usize,
    lookup_k: usize,
    verify: &[usize],
    expert_args: &ExpertArgs,
    cache: &CacheArgs,
) -> Result<()> {
    let chat = Chat::load(model_dir)?;
    let ids = chat.encode(&chat.render_user(prompt)?)?;
    let widest = verify.iter().copied().max().unwrap_or(0);
    let max_context = ids.len() + 2 * (tokens + draft) + 16 * verify.len() + widest + 1;
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
    let mut lookup = (lookup_k > 0).then(|| LookupRun {
        lookup: PromptLookup::new(&LOOKUP_MATCH, lookup_k),
        history: ids.clone(),
    });
    decode_phase(
        "decode (cold tiers)",
        &*model,
        &mut *session,
        &mut next,
        tokens,
        draft,
        lookup.as_mut(),
    )?;
    decode_phase(
        "decode (warm tiers)",
        &*model,
        &mut *session,
        &mut next,
        tokens,
        draft,
        lookup.as_mut(),
    )?;
    verify_sweep(&*model, &mut *session, &mut next, &ids, verify)?;
    println!("after decode: {}", model.describe());
    // The same prompt again on a fresh sequence: prefill from warm tiers.
    drop(session);
    let mut session = model.new_session(ids.len() + 1)?;
    println!("fresh sequence: {}", model.describe());
    timed_prefill("prefill (warm tiers)", &*model, &mut *session, &ids)?;
    println!("after warm prefill: {}", model.describe());
    Ok(())
}
