//! `oominf generate`: greedy decoding of one chat turn (speculative when the model
//! drafts).

use std::io::Write;
use std::path::Path;
use std::time::Instant;

use anyhow::{Context, Result};
use oominf_core::{argmax, decode_step};

use crate::chat::Chat;
use crate::load::{CacheArgs, ExpertArgs, OpenArgs, open_model};

pub fn run(
    model_dir: &Path,
    prompt: &str,
    max_tokens: usize,
    draft: usize,
    expert_args: &ExpertArgs,
    cache: &CacheArgs,
) -> Result<()> {
    let chat = Chat::load(model_dir)?;
    let ids = chat.encode(&chat.render_user(prompt)?)?;
    let stop: Vec<u32> = ["<|im_end|>", "<|endoftext|>"]
        .iter()
        .filter_map(|t| chat.token_id(t))
        .collect();

    let t0 = Instant::now();
    let max_context = ids.len() + max_tokens + draft + 1;
    let model = open_model(&OpenArgs {
        model_dir,
        max_context,
        prefill_chunk: None,
        experts: expert_args,
        cache,
    })?;
    eprintln!(
        "loaded in {:.1}s: {}; prompt {} tokens",
        t0.elapsed().as_secs_f64(),
        model.describe(),
        ids.len()
    );

    let mut session = model.new_session(max_context)?;
    let t1 = Instant::now();
    let logits = session
        .prefill(&ids, &|| false)?
        .context("prefill cancelled")?;
    eprintln!("prefill {:.2}s", t1.elapsed().as_secs_f64());
    let t2 = Instant::now();
    let mut out = Vec::new();
    let (mut drafted, mut accepted) = (0, 0);
    let mut next = argmax(&logits);
    'generate: while out.len() < max_tokens && !stop.contains(&next) {
        out.push(next);
        print!("{}", chat.decode(&[next])?);
        std::io::stdout().flush()?;
        if out.len() == max_tokens {
            break;
        }
        let d = decode_step(&mut *session, next, draft, |row: &[f32], _| Ok(argmax(row)))?;
        drafted += d.drafted;
        accepted += d.accepted;
        let (last, fed) = d.tokens.split_last().expect("a step produces a token");
        for &tok in fed {
            if stop.contains(&tok) || out.len() == max_tokens {
                break 'generate;
            }
            out.push(tok);
            print!("{}", chat.decode(&[tok])?);
        }
        next = *last;
    }
    println!();
    let secs = t2.elapsed().as_secs_f64();
    let spec = if drafted > 0 {
        format!(", drafts accepted {accepted}/{drafted}")
    } else {
        String::new()
    };
    eprintln!(
        "{} tokens in {:.2}s ({:.2} tok/s{spec})",
        out.len(),
        secs,
        out.len() as f64 / secs
    );
    Ok(())
}
