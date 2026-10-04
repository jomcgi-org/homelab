//! MoE microbenchmark and oracle: one layer's MoE with every expert record resident
//! on the GPU, fused path vs the reference path, for decode and long prefill.
//!
//! Needs the GPU and a converted model:
//!
//!     OOMINF_MODEL=/path/model.oom cargo test --release -p oominf-models-qwen \
//!         --test moe_bench -- --ignored --nocapture

use std::time::Instant;

use anyhow::Result;
use oominf_cuda::{Gpu, Slice};
use oominf_models_qwen::moe::Moe;
use oominf_models_qwen::{Dims, ExpertSource, NoProbe};

/// All of a layer's records uploaded once; `fetch` just returns addresses.
struct Resident {
    layer: u32,
    records: Slice<u8>,
    base: u64,
    stride: u64,
}

impl ExpertSource for Resident {
    fn fetch(&mut self, _gpu: &Gpu, layer: u32, experts: &[u32]) -> Result<Vec<u64>> {
        assert_eq!(layer, self.layer);
        let _ = &self.records;
        Ok(experts
            .iter()
            .map(|&e| self.base + u64::from(e) * self.stride)
            .collect())
    }
}

fn rms_rel(a: &[f32], b: &[f32]) -> f64 {
    let (mut d2, mut b2) = (0f64, 0f64);
    for (&x, &y) in a.iter().zip(b) {
        d2 += (x as f64 - y as f64).powi(2);
        b2 += (y as f64).powi(2);
    }
    (d2 / b2).sqrt()
}

/// Deterministic pseudo-activations with roughly unit RMS.
fn activations(n: usize, seed: u64) -> Vec<f32> {
    let mut s = seed;
    (0..n)
        .map(|_| {
            s = s
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            ((s >> 40) as f32 / (1u64 << 24) as f32 - 0.5) * 3.4
        })
        .collect()
}

#[test]
#[ignore]
fn moe_fused_vs_reference() -> Result<()> {
    let dir = std::path::PathBuf::from(
        std::env::var("OOMINF_MODEL")
            .unwrap_or("/disks/nvme-02/src/oominf-data/models/qwen38-flash.oom".into()),
    );
    let model = oominf_format::Model::open(&dir)?;
    let d = Dims::from_config(&std::fs::read_to_string(dir.join("config.json"))?)?;
    let gpu = Gpu::new(0)?;
    let layer = 0;
    let moe = Moe::load(&gpu, &model, &d, layer)?;
    let group = model.expert_group(layer).unwrap().clone();
    let stride = group.schema.stride;
    let mut host = vec![0u8; (stride * u64::from(group.num_experts)) as usize];
    for e in 0..group.num_experts {
        let s = (u64::from(e) * stride) as usize;
        model.read_record(layer, e, &mut host[s..s + stride as usize])?;
    }
    let records = gpu.upload_bytes(&host)?;
    let base = gpu.device_ptr(&records);
    let mut src = Resident {
        layer,
        records,
        base,
        stride,
    };

    for (t, iters) in [(1usize, 50usize), (73, 10), (2048, 3)] {
        let x = gpu.upload_f32(&activations(t * d.hidden, 7 + t as u64))?;
        let mut scratch = gpu.upload_u16(&vec![0u16; t * d.hidden])?;
        let mut outs = Vec::new();
        for reference in [false, true] {
            moe.set_reference(reference);
            // Warm up, then time.
            let out = moe.forward(&gpu, &d, &x, t, &mut src, &mut scratch, &mut NoProbe)?;
            gpu.sync()?;
            let n = if reference { iters.min(3) } else { iters };
            let start = Instant::now();
            for _ in 0..n {
                moe.forward(&gpu, &d, &x, t, &mut src, &mut scratch, &mut NoProbe)?;
            }
            gpu.sync()?;
            let ms = start.elapsed().as_secs_f64() * 1e3 / n as f64;
            println!(
                "T={t:>5} {:<9} {ms:>9.3} ms per MoE layer (router + shared + routed)",
                if reference { "reference" } else { "fused" }
            );
            outs.push(gpu.download(&out)?);
        }
        let rel = rms_rel(&outs[0], &outs[1]);
        println!("T={t:>5} fused vs reference moe_out rms_rel {rel:.3e}");
        assert!(rel < 1e-5, "fused path diverges from the reference: {rel}");
    }
    moe.set_reference(false);
    Ok(())
}
