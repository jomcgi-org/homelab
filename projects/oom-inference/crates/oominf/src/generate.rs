//! `oominf generate`: greedy decoding of one chat turn (correctness tool, not the
//! serving path).

use std::io::Write;
use std::path::Path;
use std::sync::Arc;
use std::time::Instant;

use anyhow::{Context, Result};
use oominf_cuda::Gpu;
use oominf_models_qwen::{Dims, NoProbe, PREFILL_CHUNK, QwenModel};

use crate::chat::Chat;
use crate::experts::{ExpertArgs, Experts};

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

    let model = Arc::new(oominf_format::Model::open(model_dir)?);
    let dims = Dims::from_config(&std::fs::read_to_string(model_dir.join("config.json"))?)?;
    let gpu = Gpu::new(0)?;
    let t0 = Instant::now();
    let qwen = QwenModel::load(&gpu, &model, dims, None)?;
    gpu.sync()?;
    eprintln!(
        "loaded in {:.1}s; prompt {} tokens",
        t0.elapsed().as_secs_f64(),
        ids.len()
    );

    // Sequence state first, so automatic tier sizing sees the VRAM that is left.
    let mut state = qwen.new_state(&gpu, ids.len() + max_tokens)?;
    let mut experts = Experts::build(expert_args, &gpu, &model)?;
    eprintln!("{}", experts.describe());
    let t1 = Instant::now();
    let mut logits = qwen
        .prefill(&gpu, &ids, PREFILL_CHUNK, &mut state, &mut experts, &|| {
            false
        })?
        .context("prefill cancelled")?;
    eprintln!("prefill {:.2}s", t1.elapsed().as_secs_f64());
    let t2 = Instant::now();
    let mut out = Vec::new();
    for _ in 0..max_tokens {
        let row = gpu.download(&logits)?;
        let next = row
            .iter()
            .enumerate()
            .max_by(|a, b| a.1.total_cmp(b.1))
            .map(|(i, _)| i as u32)
            .unwrap();
        if stop.contains(&next) {
            break;
        }
        out.push(next);
        print!("{}", chat.decode(&[next])?);
        std::io::stdout().flush()?;
        logits = qwen.forward(&gpu, &[next], &mut state, &mut experts, &mut NoProbe, true)?;
    }
    println!();
    let secs = t2.elapsed().as_secs_f64();
    eprintln!(
        "{} tokens in {:.2}s ({:.2} tok/s, reference kernels, experts read from disk every step)",
        out.len(),
        secs,
        out.len() as f64 / secs
    );
    Ok(())
}
