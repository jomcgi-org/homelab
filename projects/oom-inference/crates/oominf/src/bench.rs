//! `oominf bench`: greedy decode with a time breakdown (expert loading vs the rest),
//! the baseline every performance change is measured against.

use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::Result;
use oominf_cuda::Gpu;
use oominf_models_qwen::{Dims, DiskExperts, ExpertSource, NoProbe, QwenModel};

use crate::chat::Chat;

/// Wraps an expert source and accounts the time and bytes it spends.
struct Timed<S> {
    inner: S,
    time: Duration,
    calls: usize,
}

impl<S: ExpertSource> ExpertSource for Timed<S> {
    fn fetch(&mut self, gpu: &Gpu, layer: u32, experts: &[u32]) -> Result<Vec<u64>> {
        gpu.sync()?;
        let t = Instant::now();
        let r = self.inner.fetch(gpu, layer, experts)?;
        gpu.sync()?;
        self.time += t.elapsed();
        self.calls += experts.len();
        Ok(r)
    }
}

pub fn run(model_dir: &Path, prompt: &str, tokens: usize) -> Result<()> {
    let chat = Chat::load(model_dir)?;
    let ids = chat.encode(&chat.render_user(prompt)?)?;
    let model = Arc::new(oominf_format::Model::open(model_dir)?);
    let dims = Dims::from_config(&std::fs::read_to_string(model_dir.join("config.json"))?)?;
    let gpu = Gpu::new(0)?;
    let qwen = QwenModel::load(&gpu, &model, dims, None)?;
    let mut experts = Timed {
        inner: DiskExperts::new(model.clone()),
        time: Duration::ZERO,
        calls: 0,
    };
    let mut state = qwen.new_state(&gpu, ids.len() + tokens + 1)?;
    let t = Instant::now();
    let mut logits = qwen.forward(&gpu, &ids, &mut state, &mut experts, &mut NoProbe, true)?;
    gpu.sync()?;
    let prefill = t.elapsed();
    println!(
        "prefill {} tokens: {:.2}s (expert loading {:.2}s over {} records)",
        ids.len(),
        prefill.as_secs_f64(),
        experts.time.as_secs_f64(),
        experts.calls
    );
    experts.time = Duration::ZERO;
    experts.calls = 0;
    let t = Instant::now();
    for _ in 0..tokens {
        let row = gpu.download(&logits)?;
        let next = row
            .iter()
            .enumerate()
            .max_by(|a, b| a.1.total_cmp(b.1))
            .map(|(i, _)| i as u32)
            .unwrap();
        logits = qwen.forward(&gpu, &[next], &mut state, &mut experts, &mut NoProbe, true)?;
    }
    gpu.sync()?;
    let total = t.elapsed().as_secs_f64();
    let per = total / tokens as f64 * 1e3;
    let load = experts.time.as_secs_f64() / tokens as f64 * 1e3;
    println!(
        "decode {tokens} tokens: {:.1} ms/token ({:.2} tok/s); expert loading {:.1} ms/token ({} records/token), everything else {:.1} ms/token",
        per,
        1e3 / per,
        load,
        experts.calls / tokens,
        per - load
    );
    Ok(())
}
