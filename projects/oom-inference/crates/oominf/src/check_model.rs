//! `oominf check-model`: runs the whole model on the GPU against the reference's
//! whole-model chain (`reference/make_fixtures.py --model-chain`).
//!
//! Two runs: **layer-isolated** feeds every layer the reference's fp32 input, so a
//! layer's error is its own; **chained** runs the model end to end. The gate is the
//! chained logits: at least as close to the fp32 reference as the reference's own
//! bf16 run is (rms error and top-1 agreement).

use std::collections::HashMap;
use std::path::Path;
use std::sync::Arc;

use anyhow::{Context, Result};
use oominf_core::{Memory, Probe, argmax};
use oominf_cuda::Gpu;
use oominf_models_qwen::{Dims, QwenModel};

use crate::check_layer::{STEPS, load_step, rms_rel_and_cos, step_tokens};
use crate::load::{CacheArgs, ExpertArgs, factory};

struct ModelProbe<'a> {
    truth: &'a HashMap<String, Vec<f32>>,
    isolate: bool,
    observed: Vec<(String, Vec<f32>)>,
}

impl Probe for ModelProbe<'_> {
    fn wants(&self, stage: &str) -> bool {
        self.truth.contains_key(stage)
    }
    fn observe(&mut self, stage: &str, data: Vec<f32>) {
        self.observed.push((stage.to_owned(), data));
    }
    fn substitute(&mut self, stage: &str) -> Option<Vec<f32>> {
        if self.isolate && stage.starts_with("layer_out.") {
            self.truth.get(stage).cloned()
        } else {
            None
        }
    }
}

fn log_softmax(row: &[f32]) -> Vec<f64> {
    let m = row.iter().copied().fold(f32::NEG_INFINITY, f32::max) as f64;
    let lse = row.iter().map(|&x| (x as f64 - m).exp()).sum::<f64>().ln() + m;
    row.iter().map(|&x| x as f64 - lse).collect()
}

/// Top-1 agreement with `truth`, and the max / mean |logprob error| over the
/// truth's top-20 token ids, per position.
fn logit_metrics(
    ours: &[f32],
    truth: &[f32],
    ids: &[f32],
    vocab: usize,
) -> (usize, usize, f64, f64) {
    let rows = truth.len() / vocab;
    let (mut agree, mut max_d, mut sum_d, mut n) = (0, 0f64, 0f64, 0usize);
    for r in 0..rows {
        let (o, t) = (
            &ours[r * vocab..(r + 1) * vocab],
            &truth[r * vocab..(r + 1) * vocab],
        );
        agree += usize::from(argmax(o) == argmax(t));
        let (lo, lt) = (log_softmax(o), log_softmax(t));
        for &id in &ids[r * 20..(r + 1) * 20] {
            let d = (lo[id as usize] - lt[id as usize]).abs();
            max_d = max_d.max(d);
            sum_d += d;
            n += 1;
        }
    }
    (agree, rows, max_d, sum_d / n.max(1) as f64)
}

pub fn run(
    model_dir: &Path,
    fixtures: &Path,
    expert_args: &ExpertArgs,
    cache: &CacheArgs,
) -> Result<bool> {
    let model = Arc::new(oominf_format::Model::open(model_dir)?);
    let mut dims = Dims::from_config(&std::fs::read_to_string(model_dir.join("config.json"))?)?;
    dims.kv = cache.kv_cache;
    dims.dense = cache.dense;
    dims.kv_host = cache.kv_host();
    let tolerances: serde_json::Value = serde_json::from_slice(
        &std::fs::read(fixtures.join("tolerances.json")).context("tolerances.json")?,
    )?;
    let budget = &tolerances["w4a16/bf16_vs_fp32"];
    let tokens = step_tokens(fixtures)?;
    let gpu = Arc::new(Gpu::new(0)?);
    let t0 = std::time::Instant::now();
    let mut qwen = QwenModel::load(&*gpu, &model, dims, None)?;
    if expert_args.host_threads() > 0 {
        let pool = oominf_cpu::HostExperts::new(expert_args.host_threads())?;
        qwen.set_host_experts(Arc::new(pool));
    }
    gpu.sync()?;
    println!("model loaded in {:.1}s", t0.elapsed().as_secs_f64());
    let vocab = qwen.vocab();
    let max_tokens: usize = tokens.iter().map(Vec::len).sum();

    let mut experts = factory::<Gpu>(expert_args, model.clone())(&gpu)?;
    println!("{}", experts.describe());
    let mut ok = true;
    for isolate in [true, false] {
        println!(
            "\n== {} ==",
            if isolate {
                "layer-isolated (each layer fed the reference input)"
            } else {
                "chained (end to end)"
            }
        );
        let mut state = qwen.new_state(&*gpu, max_tokens)?;
        for (step, ids) in STEPS.iter().zip(&tokens) {
            let truth = load_step(&fixtures.join(format!("fp32/{step}.safetensors")))?;
            let mut probe = ModelProbe {
                truth: &truth,
                isolate,
                observed: Vec::new(),
            };
            let ts = std::time::Instant::now();
            qwen.forward(&*gpu, ids, &mut state, experts.as_mut(), &mut probe, false)?;
            println!(
                "-- {step} (T={}, {:.1}s)",
                ids.len(),
                ts.elapsed().as_secs_f64()
            );
            let mut worst_layer = (String::new(), 0f64, 0f64);
            for (key, ours) in &probe.observed {
                let Some(want) = truth.get(key) else { continue };
                let (rel, cos) = rms_rel_and_cos(ours, want);
                let b = budget[*step][key.as_str()]["rms_rel"]
                    .as_f64()
                    .unwrap_or(f64::NAN);
                if key.starts_with("layer_out.") {
                    if rel / b > worst_layer.2 || worst_layer.0.is_empty() {
                        worst_layer = (key.clone(), rel, rel / b);
                    }
                    if !isolate && key.ends_with(".47") {
                        println!(
                            "   {key:<14} rms_rel {rel:.3e} cos {cos:.6}  (HF bf16 {b:.3e}, ratio {:.2})",
                            rel / b
                        );
                    }
                    continue;
                }
                println!(
                    "   {key:<14} rms_rel {rel:.3e} cos {cos:.6}  (HF bf16 {b:.3e}, ratio {:.2})",
                    rel / b
                );
                if key == "logits" {
                    let top_ids = truth.get("top_logprobs.ids").context("top_logprobs.ids")?;
                    let (agree, rows, max_d, mean_d) = logit_metrics(ours, want, top_ids, vocab);
                    let bf16 = load_step(&fixtures.join(format!("bf16/{step}.safetensors")))?;
                    let hf = bf16.get("logits").context("bf16 logits")?;
                    let (hf_agree, _, hf_max, hf_mean) = logit_metrics(hf, want, top_ids, vocab);
                    println!(
                        "   top-1 vs fp32: ours {agree}/{rows}, HF bf16 {hf_agree}/{rows}; |dlogprob| on fp32 top-20: ours max {max_d:.3} mean {mean_d:.4}, HF bf16 max {hf_max:.3} mean {hf_mean:.4}"
                    );
                    if !isolate {
                        let pass = agree >= hf_agree && rel <= b * 1.05;
                        ok &= pass;
                        if !pass {
                            println!("   <-- logits less faithful than HF bf16");
                        }
                    }
                }
            }
            println!(
                "   worst layer_out vs HF bf16 budget: {} (rms_rel {:.3e}, ratio {:.2})",
                worst_layer.0, worst_layer.1, worst_layer.2
            );
        }
    }
    let s = experts.stats();
    println!(
        "\nexpert records: {} fetched, {} computed on the host",
        s.requests, s.host_computed
    );
    Ok(ok)
}
