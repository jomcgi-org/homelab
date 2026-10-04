//! `oominf generate`: greedy decoding of one chat turn.

use std::io::Write;
use std::path::Path;
use std::time::Instant;

use anyhow::{Context, Result};

use crate::chat::Chat;
use crate::load::{ExpertArgs, OpenArgs, open_model};

/// Index of the largest logit.
pub fn argmax(logits: &[f32]) -> u32 {
    logits
        .iter()
        .enumerate()
        .max_by(|a, b| a.1.total_cmp(b.1))
        .map_or(0, |(i, _)| i as u32)
}

pub fn run(
    model_dir: &Path,
    prompt: &str,
    max_tokens: usize,
    expert_args: &ExpertArgs,
) -> Result<()> {
    let chat = Chat::load(model_dir)?;
    let ids = chat.encode(&chat.render_user(prompt)?)?;
    let stop: Vec<u32> = ["<|im_end|>", "<|endoftext|>"]
        .iter()
        .filter_map(|t| chat.token_id(t))
        .collect();

    let t0 = Instant::now();
    let max_context = ids.len() + max_tokens;
    let model = open_model(&OpenArgs {
        model_dir,
        max_context,
        prefill_chunk: None,
        experts: expert_args,
    })?;
    eprintln!(
        "loaded in {:.1}s: {}; prompt {} tokens",
        t0.elapsed().as_secs_f64(),
        model.describe(),
        ids.len()
    );

    let mut session = model.new_session(max_context)?;
    let t1 = Instant::now();
    let mut logits = session
        .prefill(&ids, &|| false)?
        .context("prefill cancelled")?;
    eprintln!("prefill {:.2}s", t1.elapsed().as_secs_f64());
    let t2 = Instant::now();
    let mut out = Vec::new();
    for _ in 0..max_tokens {
        let next = argmax(&logits);
        if stop.contains(&next) {
            break;
        }
        out.push(next);
        print!("{}", chat.decode(&[next])?);
        std::io::stdout().flush()?;
        logits = session.step(&[next])?;
    }
    println!();
    let secs = t2.elapsed().as_secs_f64();
    eprintln!(
        "{} tokens in {:.2}s ({:.2} tok/s)",
        out.len(),
        secs,
        out.len() as f64 / secs
    );
    Ok(())
}
