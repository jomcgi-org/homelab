//! `oominf check-layer`: runs one decoder layer on the GPU against reference
//! fixtures (see `reference/README.md`) and reports per-stage error next to the
//! reference's own bf16-vs-fp32 error, which is the budget.

use std::collections::HashMap;
use std::path::Path;

use anyhow::{Context, Result, bail};
use oominf_convert::safetensors::read_file;
use oominf_cuda::Gpu;
use oominf_models_qwen::{Dims, GdnState, LinearLayer, Probe};

const STEPS: [&str; 4] = ["prefill", "decode-1", "decode-2", "decode-3"];
const STAGES: [&str; 23] = [
    "attn_hc.mixed",
    "attn_hc.inject",
    "gdn.in_proj_qkv",
    "gdn.in_proj_z",
    "gdn.in_proj_b",
    "gdn.in_proj_a",
    "gdn.conv_out",
    "gdn.core_out",
    "gdn.norm_out",
    "mixer_out",
    "attn_combine_out",
    "mlp_hc.mixed",
    "mlp_hc.inject",
    "router_logits",
    "topk_ids",
    "topk_weights",
    "shared_out",
    "shared_gate_logit",
    "routed_out",
    "moe_out",
    "layer_out",
    "state.conv",
    "state.recurrent",
];

fn load_step(path: &Path) -> Result<HashMap<String, Vec<f32>>> {
    let mut out = HashMap::new();
    for (name, t) in read_file(path)? {
        let v: Vec<f32> = match t.dtype.as_str() {
            "F32" => t
                .bytes
                .as_chunks::<4>()
                .0
                .iter()
                .map(|&c| f32::from_le_bytes(c))
                .collect(),
            "I64" => t
                .bytes
                .as_chunks::<8>()
                .0
                .iter()
                .map(|&c| i64::from_le_bytes(c) as f32)
                .collect(),
            other => bail!("{name}: unsupported fixture dtype {other}"),
        };
        out.insert(name, v);
    }
    Ok(out)
}

struct CheckProbe<'a> {
    truth: &'a HashMap<String, Vec<f32>>,
    isolate: bool,
    observed: HashMap<String, Vec<f32>>,
}

impl Probe for CheckProbe<'_> {
    fn observe(&mut self, stage: &str, data: Vec<f32>) {
        self.observed.insert(stage.to_owned(), data);
    }

    fn substitute(&mut self, stage: &str) -> Option<Vec<f32>> {
        if self.isolate {
            self.truth.get(stage).cloned()
        } else {
            None
        }
    }
}

fn rms_rel_and_cos(a: &[f32], b: &[f32]) -> (f64, f64) {
    let (mut d2, mut b2, mut a2, mut ab) = (0f64, 0f64, 0f64, 0f64);
    for (&x, &y) in a.iter().zip(b) {
        let (x, y) = (x as f64, y as f64);
        d2 += (x - y) * (x - y);
        b2 += y * y;
        a2 += x * x;
        ab += x * y;
    }
    let rel = if b2 > 0.0 {
        (d2 / b2).sqrt()
    } else {
        d2.sqrt()
    };
    let cos = if a2 > 0.0 && b2 > 0.0 {
        ab / (a2.sqrt() * b2.sqrt())
    } else {
        1.0
    };
    (rel, cos)
}

/// Tokens whose top-k expert set differs.
fn topk_set_mismatches(a: &[f32], b: &[f32], k: usize) -> usize {
    a.chunks(k)
        .zip(b.chunks(k))
        .filter(|(x, y)| {
            let mut x: Vec<i64> = x.iter().map(|&v| v as i64).collect();
            let mut y: Vec<i64> = y.iter().map(|&v| v as i64).collect();
            x.sort_unstable();
            y.sort_unstable();
            x != y
        })
        .count()
}

pub fn run(model_dir: &Path, fixtures: &Path, layer: u32, mode: &str) -> Result<bool> {
    let model = oominf_format::Model::open(model_dir)?;
    let dims = Dims::from_config(&std::fs::read_to_string(model_dir.join("config.json"))?)?;
    let tolerances: serde_json::Value = serde_json::from_slice(
        &std::fs::read(fixtures.join("tolerances.json")).context("tolerances.json")?,
    )?;
    let budget = &tolerances[format!("{mode}/bf16_vs_fp32")];
    let gpu = Gpu::new(0)?;
    let start = std::time::Instant::now();
    let lin = LinearLayer::load(&gpu, &model, &dims, layer)?;
    gpu.sync()?;
    println!(
        "layer {layer} loaded in {:.1}s; mode {mode}; truth = HF fp32; budget* = worst HF bf16 vs fp32 over steps",
        start.elapsed().as_secs_f64()
    );

    let mut all_ok = true;
    for isolate in [true, false] {
        println!(
            "\n== {} ==",
            if isolate {
                "isolated (each stage fed exact reference inputs)"
            } else {
                "chained (whole layer from residual_in)"
            }
        );
        let mut state = GdnState::new(&gpu, &dims)?;
        for step in STEPS {
            let truth = load_step(&fixtures.join(format!("{mode}/fp32/{step}.safetensors")))?;
            let input = truth
                .get("residual_in")
                .context("fixture lacks residual_in")?;
            let t = input.len() / dims.residual();
            let mut probe = CheckProbe {
                truth: &truth,
                isolate,
                observed: HashMap::new(),
            };
            let x = gpu.upload_f32(input)?;
            lin.forward(&gpu, &x, t, &mut state, &mut probe)?;
            println!("-- {step} (T={t})");
            println!(
                "   {:<20} {:>10} {:>12} {:>12} {:>7}",
                "stage", "rms_rel", "cosine", "budget*", "ratio"
            );
            for stage in STAGES {
                let (Some(ours), Some(want)) = (probe.observed.get(stage), truth.get(stage)) else {
                    continue;
                };
                if ours.len() != want.len() {
                    bail!("{stage}: {} values, fixture has {}", ours.len(), want.len());
                }
                if stage == "topk_ids" {
                    let hf = topk_set_mismatches(ours, want, dims.top_k);
                    println!(
                        "   {:<20} {:>10} tokens with a different expert set (of {t})",
                        stage, hf
                    );
                    continue;
                }
                let (rel, cos) = rms_rel_and_cos(ours, want);
                // The reference's worst bf16 error for this stage over all steps: a
                // per-step budget is meaningless for single-token, few-element stages.
                let b = STEPS
                    .iter()
                    .filter_map(|s| budget[*s][stage]["rms_rel"].as_f64())
                    .fold(f64::NAN, f64::max);
                let ratio = rel / b;
                // Chained errors legitimately include routing flips at near-ties, so
                // only isolated runs are held to the budget.
                let bad = isolate && ratio > 1.5 && rel > 1e-6;
                all_ok &= !bad;
                println!(
                    "   {:<20} {:>10.3e} {:>12.8} {:>12.3e} {:>7.2}{}",
                    stage,
                    rel,
                    cos,
                    b,
                    ratio,
                    if bad { "  <-- over budget" } else { "" }
                );
            }
        }
    }
    Ok(all_ok)
}
