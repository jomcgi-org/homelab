//! A saved and restored sequence must continue exactly like the original: same
//! logits, bit for bit, and the same drafts. Needs a GPU and a converted model:
//!
//!     OOMINF_MODEL=/path/model.oom cargo test --release -p oominf-models-qwen \
//!         --test snapshot -- --ignored --nocapture
//!
//! Host compute is off (`host_threads: 0`): experts computed on the CPU round
//! differently and are chosen by cache timing, which would make two runs differ.

use std::sync::Arc;

use oominf_core::{KvFormat, Model, argmax};
use oominf_cuda::Gpu;
use oominf_models_qwen::{Options, open};
use oominf_tiers::policy;

fn model(kv: KvFormat) -> Box<dyn Model> {
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
                0,
                &policies,
                &oominf_tiers::host::IoConfig::default(),
            )
        }),
    )
    .unwrap()
}

#[test]
#[ignore = "needs a GPU and OOMINF_MODEL"]
fn restored_sequence_continues_identically() {
    let m = model(KvFormat::Turbo {
        k_bits: 8,
        v_bits: 6,
    });
    // Long enough for layer-major prefill and several QSA indexer blocks.
    let prompt: Vec<u32> = (0..1100u32).map(|i| 300 + (i * 7919) % 20_000).collect();
    let mut a = m.new_session(2048).unwrap();
    let logits = a.prefill(&prompt, &|| false).unwrap().unwrap();
    let mut saved = Vec::new();
    a.save(&mut saved).unwrap();
    eprintln!("snapshot of {} tokens: {} bytes", prompt.len(), saved.len());

    let mut b = m.new_session(2048).unwrap();
    b.load(&mut saved.as_slice()).unwrap();
    assert_eq!(b.len(), a.len());

    let mut next = argmax(&logits);
    for step in 0..24 {
        assert_eq!(
            a.draft(next, 2).unwrap(),
            b.draft(next, 2).unwrap(),
            "drafts differ at step {step}"
        );
        let (la, lb) = (a.step(&[next]).unwrap(), b.step(&[next]).unwrap());
        let differing = la
            .iter()
            .zip(&lb)
            .filter(|(x, y)| x.to_bits() != y.to_bits())
            .count();
        assert_eq!(differing, 0, "{differing} logits differ at step {step}");
        next = argmax(&la);
    }
}
