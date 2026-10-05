//! Prefix checkpoints: a sequence rewound to a checkpoint and fed a different
//! continuation must match, bit for bit, a sequence that prefilled just that prefix
//! and then the continuation, also after its checkpoints were saved and restored. Needs a GPU and a converted model:
//!
//!     OOMINF_MODEL=/path/model.oom cargo test --release -p oominf-models-qwen \
//!         --test checkpoint -- --ignored --nocapture
//!
//! Host compute is off (`host_threads: 0`), as in the snapshot test.

use std::sync::Arc;

use oominf_core::{KvFormat, Model, argmax};
use oominf_cuda::Gpu;
use oominf_models_qwen::{Options, open};
use oominf_tiers::policy;

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
            host_threads: 0,
            kv: KvFormat::Turbo {
                k_bits: 8,
                v_bits: 6,
            },
            dense: oominf_core::DenseFormat::Bf16,
            expert_precision: oominf_core::ExpertPrecision::Exact,
            kv_host: false,
        },
        Box::new(move |b| {
            let policies = || Ok((policy::parse("lru")?, policy::parse("lru")?));
            oominf_tiers::tiered_for_model(b, &experts_files, 8.0, 16.0, true, 0, &policies)
        }),
    )
    .unwrap()
}

fn tokens(n: usize, seed: u32) -> Vec<u32> {
    (0..n as u32)
        .map(|i| 300 + (i.wrapping_mul(7919).wrapping_add(seed * 104_729)) % 20_000)
        .collect()
}

/// Largest |a - b| over the largest |b|.
fn rel_diff(a: &[f32], b: &[f32]) -> f32 {
    let d = a
        .iter()
        .zip(b)
        .map(|(x, y)| (x - y).abs())
        .fold(0f32, f32::max);
    d / b.iter().map(|y| y.abs()).fold(0f32, f32::max)
}

#[test]
#[ignore = "needs a GPU and OOMINF_MODEL"]
fn rewound_prefix_continues_exactly() {
    let m = model();
    // A shared prefix of several chunks (an odd length, so the checkpoint is not
    // on a chunk or indexer-block boundary), then two different continuations.
    let p = 1537;
    let prefix = tokens(p, 1);
    let one: Vec<u32> = prefix.iter().copied().chain(tokens(600, 2)).collect();
    let two: Vec<u32> = prefix.iter().copied().chain(tokens(700, 3)).collect();

    let mut a = m.new_session(4096).unwrap();
    a.plan_checkpoints(&[p]);
    a.prefill(&one, &|| false).unwrap().unwrap();
    assert_eq!(a.checkpoints(), vec![p]);
    let mut saved = Vec::new();
    a.save(&mut saved).unwrap();

    a.rewind_to(p).unwrap();
    assert_eq!(a.len(), p);
    let resumed = a.prefill(&two[p..], &|| false).unwrap().unwrap();

    // Exact: a sequence that prefilled just the prefix and then the new suffix
    // runs the same steps as the rewound one.
    let mut x = m.new_session(4096).unwrap();
    x.prefill(&prefix, &|| false).unwrap().unwrap();
    let stepwise = x.prefill(&two[p..], &|| false).unwrap().unwrap();
    let differing = resumed
        .iter()
        .zip(&stepwise)
        .filter(|(a, b)| a.to_bits() != b.to_bits())
        .count();
    assert_eq!(
        differing, 0,
        "{differing} logits differ from prefix-then-suffix"
    );

    // One prefill of the whole new prompt runs other step shapes: it rounds
    // differently (as changing the prefill chunk does), so this is reported only.
    let mut b = m.new_session(4096).unwrap();
    b.plan_checkpoints(&[p]);
    let fresh = b.prefill(&two, &|| false).unwrap().unwrap();
    eprintln!(
        "rewound vs one prefill: relative logit difference {:.2e}, same next token: {}",
        rel_diff(&resumed, &fresh),
        argmax(&resumed) == argmax(&fresh)
    );

    // The saved sequence's checkpoint restores exactly: rewinding it gives the
    // same logits, bit for bit, as rewinding the original did.
    let mut c = m.new_session(4096).unwrap();
    c.load(&mut saved.as_slice()).unwrap();
    assert_eq!(c.checkpoints(), vec![p]);
    c.rewind_to(p).unwrap();
    let restored = c.prefill(&two[p..], &|| false).unwrap().unwrap();
    let differing = restored
        .iter()
        .zip(&resumed)
        .filter(|(x, y)| x.to_bits() != y.to_bits())
        .count();
    assert_eq!(
        differing, 0,
        "{differing} logits differ after save, load and rewind"
    );
}
