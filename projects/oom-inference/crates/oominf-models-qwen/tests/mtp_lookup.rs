//! Steps drafted by prompt lookup do not call the MTP draft head; its next call
//! must catch up on every token they kept, so it drafts as if it had run on every
//! step. Needs a GPU and a converted model with its MTP head:
//!
//!     OOMINF_MODEL=/path/model.oom cargo test --release -p oominf-models-qwen \
//!         --test mtp_lookup -- --ignored --nocapture
//!
//! A reference sequence decodes one token per step and calls the draft head before
//! every step. A second sequence feeds the same tokens in lookup-sized steps (some
//! with rejected tail tokens that are rewound) and calls the draft head only after
//! each run. Its drafts must equal the reference's at the same positions. The
//! residuals the draft head reads come from wider steps, which differ from
//! one-token steps in summation order only, so a draft token may differ where the
//! head's top two logits nearly tie: at most one in [`TOLERATED`] draft tokens.
//! Restarting the head's cache instead (what it did before it kept residuals
//! across lookup steps) changed 13 of the 48 draft tokens compared here; catching
//! up changed none.

use std::sync::Arc;

use oominf_core::{Model, Session, argmax};
use oominf_cuda::Gpu;
use oominf_models_qwen::{Options, open};
use oominf_tiers::policy;

const TOKENS: usize = 480;
const DRAFT: usize = 3;
/// At most one differing draft token per this many compared.
const TOLERATED: usize = 20;

fn model() -> Box<dyn Model> {
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
            // Host compute rounds differently and is chosen by cache timing.
            host_threads: 0,
            kv: oominf_core::KvFormat::F32,
            dense: oominf_core::DenseFormat::Bf16,
            expert_precision: oominf_core::ExpertPrecision::Exact,
            attention_precision: oominf_core::AttentionPrecision::Exact,
            kv_host: false,
        },
        Box::new(move |b| {
            let policies = || Ok((policy::parse("lru")?, policy::parse("lru")?));
            oominf_tiers::tiered_for_model(b, &experts_files, 8.0, 16.0, true, 0, &policies)
        }),
    )
    .unwrap()
}

/// Lookup runs between draft-head calls: `(tokens kept, tokens fed per step,
/// rejected tail tokens per step)`. The 40- and 120-token runs are longer than the
/// head used to catch up on.
const RUNS: [(usize, usize, usize); 8] = [
    (6, 3, 2),
    (21, 7, 0),
    (40, 8, 0),
    (9, 3, 4),
    (120, 8, 0),
    (2, 2, 6),
    (15, 5, 1),
    (30, 6, 3),
];

#[test]
#[ignore = "needs a GPU and OOMINF_MODEL"]
fn mtp_drafts_after_lookup_steps_match_stepped_drafts() {
    let m = model();
    let prompt: Vec<u32> = (0..64u32).map(|i| 300 + (i * 523) % 20_000).collect();

    // Reference: one token per step, the draft head called before each.
    let mut a = m.new_session(prompt.len() + TOKENS + 16).unwrap();
    let mut out = vec![argmax(&a.prefill(&prompt, &|| false).unwrap().unwrap())];
    let mut reference = Vec::with_capacity(TOKENS);
    while out.len() < TOKENS {
        let next = *out.last().unwrap();
        reference.push(a.draft(next, DRAFT).unwrap());
        out.push(argmax(&a.step(&[next]).unwrap()));
    }

    // Lookup-style: runs of multi-token steps without the draft head, which is
    // called (and its draft verified away by a one-token step) between runs.
    let mut b = m.new_session(prompt.len() + TOKENS + 16).unwrap();
    b.prefill(&prompt, &|| false).unwrap();
    let (mut i, mut compared, mut differing) = (0, 0, 0);
    let mut check = |b: &mut Box<dyn Session>, i: usize| {
        let d = b.draft(out[i], DRAFT).unwrap();
        let diff = d.iter().zip(&reference[i]).filter(|(x, y)| x != y).count();
        eprintln!(
            "position {i}: drafts {d:?}, reference {:?}{}",
            reference[i],
            if diff > 0 { "  DIFFERS" } else { "" }
        );
        compared += DRAFT;
        differing += diff;
    };
    for &(run, width, rejected) in RUNS.iter().cycle() {
        if i + run + 1 >= out.len() {
            break;
        }
        check(&mut b, i);
        b.step(&[out[i]]).unwrap();
        i += 1;
        let end = i + run;
        while i < end {
            let kept = width.min(end - i);
            let mut feed = out[i..i + kept].to_vec();
            // Wrong tokens after the kept ones, as a partly rejected lookup draft.
            feed.extend((0..rejected).map(|j| 1000 + j as u32));
            b.step_all(&feed).unwrap();
            b.rewind(rejected).unwrap();
            i += kept;
        }
    }
    check(&mut b, i);
    eprintln!("{differing} of {compared} draft tokens differ");
    assert!(
        differing * TOLERATED <= compared,
        "{differing} of {compared} draft tokens differ from drafts made on every step"
    );
}
