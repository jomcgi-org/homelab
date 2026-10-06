//! Greedy speculative decoding (MTP drafts verified by the model) must produce the
//! same tokens as decoding one token at a time. Needs a GPU and a converted model
//! with its MTP head:
//!
//!     OOMINF_MODEL=/path/model.oom cargo test --release -p oominf-models-qwen \
//!         --test speculative -- --ignored --nocapture
//!
//! A verify step evaluates several tokens at once, so its arithmetic differs from
//! one-token steps in summation order only. A divergence is therefore accepted only
//! where the one-token run's top two logits nearly tie, and is reported with that gap.

use std::sync::Arc;

use oominf_core::{Model, argmax, decode_step};
use oominf_cuda::Gpu;
use oominf_models_qwen::{Options, open};
use oominf_tiers::policy;

const TOKENS: usize = 160;
/// Largest top-two logit gap at which a divergence counts as a near tie.
const NEAR_TIE: f32 = 0.05;

fn top2_gap(row: &[f32]) -> f32 {
    let (mut a, mut b) = (f32::NEG_INFINITY, f32::NEG_INFINITY);
    for &v in row {
        if v > a {
            b = a;
            a = v;
        } else if v > b {
            b = v;
        }
    }
    a - b
}

/// Greedy tokens after `prompt`, drafting up to `draft` per step, with the logits
/// each token was chosen from.
fn generate(model: &dyn Model, prompt: &[u32], draft: usize) -> (Vec<u32>, Vec<f32>) {
    let mut s = model.new_session(prompt.len() + TOKENS + 8).unwrap();
    let logits = s.prefill(prompt, &|| false).unwrap().unwrap();
    let (mut out, mut gaps) = (vec![argmax(&logits)], vec![top2_gap(&logits)]);
    let (mut drafted, mut accepted) = (0, 0);
    while out.len() < TOKENS {
        let next = *out.last().unwrap();
        let d = decode_step(&mut *s, next, draft, |row: &[f32], _| Ok(argmax(row))).unwrap();
        drafted += d.drafted;
        accepted += d.accepted;
        for (t, row) in d.tokens.iter().zip(&d.rows) {
            out.push(*t);
            gaps.push(top2_gap(row));
        }
    }
    if drafted > 0 {
        eprintln!("  draft {draft}: accepted {accepted}/{drafted}");
    }
    out.truncate(TOKENS);
    gaps.truncate(TOKENS);
    (out, gaps)
}

#[test]
#[ignore = "needs a GPU and OOMINF_MODEL"]
fn speculative_greedy_matches_one_token_steps() {
    let dir = std::path::PathBuf::from(
        std::env::var_os("OOMINF_MODEL").expect("set OOMINF_MODEL to a converted model"),
    );
    let files = Arc::new(oominf_format::Model::open(&dir).unwrap());
    let gpu = Arc::new(Gpu::new(0).unwrap());
    let experts_files = files.clone();
    let model = open(
        gpu,
        files,
        Options {
            max_context: 4096,
            prefill_chunk: oominf_models_qwen::PREFILL_CHUNK,
            host_threads: 8,
            kv: oominf_core::KvFormat::F32,
            dense: oominf_core::DenseFormat::Bf16,
            expert_precision: oominf_core::ExpertPrecision::Exact,
            attention_precision: oominf_core::AttentionPrecision::Exact,
            kv_host: false,
        },
        Box::new(move |b, _| {
            let policies = || Ok((policy::parse("lru")?, policy::parse("lru")?));
            oominf_tiers::tiered_for_model(
                b,
                &experts_files,
                &oominf_tiers::TierBudget {
                    vram_gib: 8.0,
                    host_gib: 16.0,
                    host_stage_slots: None,
                },
                true,
                6,
                &policies,
                &oominf_tiers::host::IoConfig::default(),
            )
        }),
    )
    .unwrap();
    // Token-id prompts from plain text spans of the vocabulary's common range.
    let prompts: Vec<Vec<u32>> = [11u32, 523, 7919]
        .iter()
        .map(|&seed| (0..48u32).map(|i| 300 + (i * seed) % 20_000).collect())
        .collect();
    let mut near_ties = 0;
    for (p, prompt) in prompts.iter().enumerate() {
        let (base, gaps) = generate(&*model, prompt, 0);
        for draft in [1, 2] {
            let (spec, _) = generate(&*model, prompt, draft);
            match base.iter().zip(&spec).position(|(a, b)| a != b) {
                None => eprintln!("prompt {p}, draft {draft}: identical over {TOKENS} tokens"),
                Some(i) => {
                    eprintln!(
                        "prompt {p}, draft {draft}: first divergence at token {i}, top-two gap {:.4}",
                        gaps[i]
                    );
                    assert!(
                        gaps[i] < NEAR_TIE,
                        "prompt {p}, draft {draft}: diverged at token {i} where the gap is {}",
                        gaps[i]
                    );
                    near_ties += 1;
                }
            }
        }
    }
    eprintln!("{near_ties} near-tie divergences");
}
