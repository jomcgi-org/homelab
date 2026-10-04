//! `oominf score`: teacher-forced next-token distributions over the last `tail`
//! tokens of a long document, the yardstick for runtime precision modes (for
//! example a compressed KV cache) judged by outcome rather than per-layer error.
//!
//! The document is tokenized as plain text (no chat template, so every scored
//! position is document text), prefilled up to its tail, and the tail is fed one
//! token at a time, so every scored position reads the cache the way generation
//! does. Each position's logits are written (`--out`) or compared with a reference
//! run (`--against`): KL divergence from the reference distribution, top-1
//! agreement, and the perplexity of the document's actual tokens under both.
//!
//! File format: `u32` header length, a JSON header binding the logits to their
//! inputs (token-id and checkpoint checksums, tail, the run's KV cache format),
//! then `rows x vocab` `f32` logits, little-endian. A comparison refuses a
//! reference made from other tokens or another checkpoint.

use std::fs::File;
use std::io::{BufReader, BufWriter, Read, Write};
use std::path::Path;
use std::time::Instant;

use anyhow::{Context, Result, ensure};
use oominf_core::argmax;

use crate::chat::Chat;
use crate::load::{CacheArgs, ExpertArgs, OpenArgs, open_model};

/// `log_softmax(logits)` in f64.
fn log_softmax(logits: &[f32]) -> Vec<f64> {
    let max = logits.iter().copied().fold(f32::NEG_INFINITY, f32::max) as f64;
    let sum: f64 = logits.iter().map(|&l| (l as f64 - max).exp()).sum();
    let lse = max + sum.ln();
    logits.iter().map(|&l| l as f64 - lse).collect()
}

fn read_u32(r: &mut impl Read) -> Result<u32> {
    let mut b = [0u8; 4];
    r.read_exact(&mut b)?;
    Ok(u32::from_le_bytes(b))
}

/// Checksum of the files that identify a converted checkpoint (its index lists
/// every tensor's checksum).
fn checkpoint_id(model_dir: &Path) -> Result<String> {
    let mut bytes = std::fs::read(model_dir.join(oominf_format::INDEX_NAME))?;
    bytes.extend(std::fs::read(model_dir.join("config.json"))?);
    Ok(oominf_format::checksum(&bytes))
}

/// Running comparison against a reference run.
#[derive(Default)]
struct Against {
    kl: Vec<f64>,
    top1_agree: usize,
    ref_nll: f64,
}

/// What to score and where its logits go.
pub struct Options<'a> {
    /// Tokens at the end of the document to score.
    pub tail: usize,
    /// Write each scored position's logits here.
    pub out: Option<&'a Path>,
    /// Compare with logits written by an earlier run.
    pub against: Option<&'a Path>,
    /// Prompt tokens per prefill chunk (`None`: the model family's default).
    pub prefill_chunk: Option<usize>,
}

pub fn run(
    model_dir: &Path,
    text: &str,
    opts: &Options,
    expert_args: &ExpertArgs,
    cache: &CacheArgs,
) -> Result<()> {
    let (tail, out, against) = (opts.tail, opts.out, opts.against);
    let chat = Chat::load(model_dir)?;
    let ids = chat.encode(text)?;
    ensure!(
        tail >= 1 && tail < ids.len(),
        "tail {tail} must be shorter than the {}-token prompt",
        ids.len()
    );
    let model = open_model(&OpenArgs {
        model_dir,
        max_context: ids.len() + 1,
        prefill_chunk: opts.prefill_chunk,
        experts: expert_args,
        cache,
    })?;
    let vocab = model.vocab();
    let start = ids.len() - tail;
    let tokens_id = oominf_format::checksum(
        &ids.iter()
            .flat_map(|t| t.to_le_bytes())
            .collect::<Vec<u8>>(),
    );
    let header = serde_json::json!({
        "version": 1,
        "framing": "plain text, last `tail` tokens",
        "vocab": vocab,
        "tail": tail,
        "tokens": ids.len(),
        "tokens_xxh3": tokens_id,
        "checkpoint_xxh3": checkpoint_id(model_dir)?,
        "kv_cache": format!("{:?}", cache.kv_cache),
    });
    let mut writer = out
        .map(|p| -> Result<_> {
            let mut w = BufWriter::new(File::create(p)?);
            let h = serde_json::to_vec(&header)?;
            w.write_all(&(h.len() as u32).to_le_bytes())?;
            w.write_all(&h)?;
            Ok(w)
        })
        .transpose()?;
    let mut reference = against
        .map(|p| -> Result<_> {
            let mut r = BufReader::new(File::open(p).with_context(|| p.display().to_string())?);
            let mut h = vec![0u8; read_u32(&mut r)? as usize];
            r.read_exact(&mut h)?;
            let theirs: serde_json::Value = serde_json::from_slice(&h)?;
            for key in [
                "version",
                "framing",
                "vocab",
                "tail",
                "tokens_xxh3",
                "checkpoint_xxh3",
            ] {
                ensure!(
                    theirs[key] == header[key],
                    "reference {} differs: {} vs {}",
                    key,
                    theirs[key],
                    header[key]
                );
            }
            println!("reference: KV cache {}", theirs["kv_cache"]);
            Ok(r)
        })
        .transpose()?;
    let mut cmp = Against::default();
    let mut nll = 0f64;
    let mut ref_row = vec![0f32; vocab];

    let mut session = model.new_session(ids.len() + 1)?;
    let t = Instant::now();
    let mut logits = session
        .prefill(&ids[..start], &|| false)?
        .context("prefill cancelled")?;
    let prefill_s = t.elapsed().as_secs_f64();
    let t = Instant::now();
    for (i, &target) in ids[start..].iter().enumerate() {
        let lp = log_softmax(&logits);
        nll -= lp[target as usize];
        if let Some(w) = writer.as_mut() {
            for &l in &logits {
                w.write_all(&l.to_le_bytes())?;
            }
        }
        if let Some(r) = reference.as_mut() {
            let mut buf = vec![0u8; vocab * 4];
            r.read_exact(&mut buf)?;
            for (dst, b) in ref_row.iter_mut().zip(buf.as_chunks::<4>().0) {
                *dst = f32::from_le_bytes(*b);
            }
            let rp = log_softmax(&ref_row);
            cmp.kl
                .push(rp.iter().zip(&lp).map(|(&a, &b)| a.exp() * (a - b)).sum());
            cmp.top1_agree += usize::from(argmax(&ref_row) == argmax(&logits));
            cmp.ref_nll -= rp[target as usize];
        }
        if i + 1 < tail {
            logits = session.step(&[target])?;
        }
    }
    if let Some(mut w) = writer {
        w.flush()?;
    }
    println!(
        "{} tokens, scored the last {tail} (prefill {prefill_s:.1}s, {:.1} ms/token): perplexity {:.4}",
        ids.len(),
        t.elapsed().as_secs_f64() / tail as f64 * 1e3,
        (nll / tail as f64).exp()
    );
    if reference.is_some() {
        let n = cmp.kl.len() as f64;
        let mut sorted = cmp.kl.clone();
        sorted.sort_by(f64::total_cmp);
        println!(
            "vs reference: KL mean {:.3e}, p99 {:.3e}, max {:.3e}; top-1 agreement {:.2}%; reference perplexity {:.4}",
            cmp.kl.iter().sum::<f64>() / n,
            sorted[((n * 0.99) as usize).min(sorted.len() - 1)],
            sorted.last().copied().unwrap_or(0.0),
            100.0 * cmp.top1_agree as f64 / n,
            (cmp.ref_nll / n).exp()
        );
    }
    Ok(())
}
