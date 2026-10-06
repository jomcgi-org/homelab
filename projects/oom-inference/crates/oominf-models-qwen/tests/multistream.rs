//! A batched step over several sequences (`Model::step_many`) must give each
//! sequence what stepping it alone gives. Needs a GPU and a converted model:
//!
//!     OOMINF_MODEL=/path/model.oom cargo test --release -p oominf-models-qwen \
//!         --test multistream -- --ignored --nocapture
//!
//! Three sequences of different lengths (one prefilled layer by layer) take steps
//! of different widths together (next token plus MTP drafts, some sequences
//! sitting a step out), rewinding rejected drafts each on its own, against twins
//! that take the same steps alone.
//!
//! Up to 4 tokens a step's dense GEMVs reduce each row the same way whatever the
//! row count, so until the first wider step the twins match to the bit (gate
//! 1e-4 of the row's largest logit, same argmax): a wrong row, position or state
//! shows up as order-one differences. A wider step runs wider GEMV instances
//! that split the reduction differently, the summation-order change a draft
//! verification step makes against one-token steps: that step still matches
//! within 1.2e-6 (same gate). Its rounding then lives on in the twins' states,
//! and the compressed KV cache can turn it into a different codebook index, so
//! later steps drift apart as speculative and one-token decoding do (measured up
//! to 3e-2 of the largest logit): there the gate is 1e-1 and the argmax must
//! agree unless the twin's top two logits nearly tie (as in the speculative
//! test). Steps stay within 16 tokens, the decode GEMV's limit; wider ones run
//! cuBLAS on bf16-rounded activations. Host expert compute is off: which experts
//! run on the CPU depends on copy timing, and its rounding differs from the
//! GPU's, so with it on neither run is reproducible even alone.

use std::sync::Arc;

use oominf_core::{Feed, Model, Session, argmax};
use oominf_cuda::Gpu;
use oominf_models_qwen::{Options, open};
use oominf_tiers::policy;

/// Largest difference allowed, relative to the row's largest absolute logit,
/// before and after a step wider than [`NARROW`] tokens has run.
const REL_TOL: f32 = 1e-4;
const REL_TOL_WIDE: f32 = 1e-1;
const NARROW: usize = 4;
/// Largest top-two logit gap at which an argmax difference counts as a near tie.
const NEAR_TIE: f32 = 0.05;

fn model(kv: oominf_core::KvFormat) -> Box<dyn Model> {
    let dir = std::path::PathBuf::from(
        std::env::var_os("OOMINF_MODEL").expect("set OOMINF_MODEL to a converted model"),
    );
    let files = Arc::new(oominf_format::Model::open(&dir).unwrap());
    let gpu = Arc::new(Gpu::new(0).unwrap());
    let experts_files = files.clone();
    open(
        gpu,
        files,
        Options {
            max_context: 4096,
            prefill_chunk: oominf_models_qwen::PREFILL_CHUNK,
            host_threads: 0,
            kv,
            dense: oominf_core::DenseFormat::Bf16,
            expert_precision: oominf_core::ExpertPrecision::Exact,
            attention_precision: oominf_core::AttentionPrecision::Exact,
            kv_host: false,
        },
        Box::new(move |b| {
            let policies = || Ok((policy::parse("lru")?, policy::parse("lru")?));
            oominf_tiers::tiered_for_model(b, &experts_files, 8.0, 16.0, true, 6, &policies)
        }),
    )
    .unwrap()
}

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

/// Largest absolute difference between `a` and `b`, relative to `a`'s largest
/// absolute value, row by row of `vocab`; and whether every row's argmax agrees
/// (with `near_ties`, also where `a`'s top two logits nearly tie).
fn compare(a: &[f32], b: &[f32], vocab: usize, near_ties: bool) -> (f32, bool) {
    assert_eq!(a.len(), b.len(), "logit rows differ in count");
    let mut worst = 0f32;
    let mut same = true;
    for (ra, rb) in a.chunks(vocab).zip(b.chunks(vocab)) {
        let scale = ra.iter().fold(0f32, |m, v| m.max(v.abs())).max(1e-6);
        let diff = ra
            .iter()
            .zip(rb)
            .fold(0f32, |m, (x, y)| m.max((x - y).abs()));
        worst = worst.max(diff / scale);
        same &= argmax(ra) == argmax(rb) || (near_ties && top2_gap(ra) < NEAR_TIE);
    }
    (worst, same)
}

fn run(kv: oominf_core::KvFormat) {
    let model = model(kv);
    let vocab = model.vocab();
    // Different lengths; the longest prefills layer by layer (more than one chunk).
    let prompts: Vec<Vec<u32>> = [(11u32, 40usize), (523, 300), (7919, 900)]
        .iter()
        .map(|&(seed, n)| (0..n as u32).map(|i| 300 + (i * seed) % 20_000).collect())
        .collect();
    let mut alone: Vec<Box<dyn Session>> = Vec::new();
    let mut batched: Vec<Box<dyn Session>> = Vec::new();
    let mut next = Vec::new();
    for p in &prompts {
        let mut a = model.new_session(4096).unwrap();
        let mut b = model.new_session(4096).unwrap();
        let la = a.prefill(p, &|| false).unwrap().unwrap();
        let lb = b.prefill(p, &|| false).unwrap().unwrap();
        assert_eq!(la, lb, "prefill is not reproducible");
        next.push(argmax(&la));
        alone.push(a);
        batched.push(b);
    }
    // Per round, each sequence's step width (next token plus drafts), 0 to sit out.
    let rounds: &[[usize; 3]] = &[
        [1, 1, 1],
        [1, 2, 1],
        [2, 0, 2],
        [0, 3, 1],
        [1, 1, 2],
        [1, 3, 2],
        [2, 1, 1],
        [0, 2, 3],
        [4, 0, 1],
        [3, 3, 3],
        [1, 1, 1],
        [2, 4, 2],
        [1, 0, 0],
        [5, 5, 5],
        [1, 2, 1],
    ];
    let (mut worst, mut worst_wide) = (0f32, 0f32);
    // Once a wide step has run, the twins' states carry its rounding.
    let mut drifted = false;
    for (r, widths) in rounds.iter().enumerate() {
        let mut feeds_tokens: Vec<(usize, Vec<u32>)> = Vec::new();
        for (i, &w) in widths.iter().enumerate() {
            if w == 0 {
                continue;
            }
            let da = alone[i].draft(next[i], w - 1).unwrap();
            let db = batched[i].draft(next[i], w - 1).unwrap();
            // After a wide step the draft head reads residuals that carry its
            // rounding (logits move by up to ~1 on the 900-token sequence), so a
            // near tie can flip. Both twins verify `da`, so logit parity below does
            // not depend on it; tests/mtp_lookup.rs checks the draft state exactly.
            if !drifted {
                assert_eq!(da, db, "round {r}, sequence {i}: drafts differ");
            }
            let mut feed = vec![next[i]];
            feed.extend(da);
            feeds_tokens.push((i, feed));
        }
        let expect: Vec<Vec<f32>> = feeds_tokens
            .iter()
            .map(|(i, f)| alone[*i].step_all(f).unwrap())
            .collect();
        let got = {
            let mut sessions: Vec<Option<&mut Box<dyn Session>>> =
                batched.iter_mut().map(Some).collect();
            let mut feeds: Vec<Feed<'_>> = feeds_tokens
                .iter()
                .map(|(i, f)| Feed {
                    session: &mut **sessions[*i].take().unwrap(),
                    tokens: f,
                })
                .collect();
            model.step_many(&mut feeds).unwrap()
        };
        let width: usize = feeds_tokens.iter().map(|(_, f)| f.len()).sum();
        let wide = drifted;
        drifted |= width > NARROW;
        let tol = if wide { REL_TOL_WIDE } else { REL_TOL };
        for (((i, feed), e), g) in feeds_tokens.iter().zip(&expect).zip(&got) {
            let (rel, same) = compare(e, g, vocab, wide);
            eprintln!(
                "round {r} (width {width}), sequence {i}: {} tokens, max relative diff {rel:.2e}",
                feed.len()
            );
            assert!(same, "round {r}, sequence {i}: argmax differs");
            assert!(
                rel <= tol,
                "round {r}, sequence {i}: relative diff {rel} over {tol}"
            );
            if wide {
                worst_wide = worst_wide.max(rel);
            } else {
                worst = worst.max(rel);
            }
            // Greedy acceptance from the reference logits; both twins rewind alike.
            let mut keep = 1;
            while keep < feed.len() && argmax(&e[(keep - 1) * vocab..keep * vocab]) == feed[keep] {
                keep += 1;
            }
            let rewind = feed.len() - keep;
            alone[*i].rewind(rewind).unwrap();
            batched[*i].rewind(rewind).unwrap();
            next[*i] = argmax(&e[(keep - 1) * vocab..keep * vocab]);
            assert_eq!(alone[*i].len(), batched[*i].len());
        }
    }
    // After rewinds the twins must still agree: one more step each, alone (their
    // states carry the wide steps' rounding).
    for i in 0..prompts.len() {
        let e = alone[i].step_all(&[next[i]]).unwrap();
        let g = batched[i].step_all(&[next[i]]).unwrap();
        let (rel, same) = compare(&e, &g, vocab, true);
        eprintln!("after rewinds, sequence {i}: max relative diff {rel:.2e}");
        assert!(
            same && rel <= REL_TOL_WIDE,
            "sequence {i} diverged after rewinds"
        );
    }
    eprintln!(
        "{kv:?}: worst relative diff {worst:.2e} up to the first step wider than {NARROW} tokens, {worst_wide:.2e} after it"
    );
}

#[test]
#[ignore = "needs a GPU and OOMINF_MODEL"]
fn batched_steps_match_steps_alone() {
    run(oominf_core::KvFormat::parse("k8v6").unwrap());
}
