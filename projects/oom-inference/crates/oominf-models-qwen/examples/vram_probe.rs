//! Prints device memory in use across prefill, decode and state reset, to find
//! buffers that outlive their step. `cargo run --release --example vram_probe -- <model.oom>`

use std::sync::Arc;

use anyhow::Result;
use oominf_cuda::Gpu;
use oominf_models_qwen::{Dims, DiskExperts, NoProbe, QwenModel};

fn used_mib(gpu: &Gpu) -> f64 {
    let (free, total) = gpu.ctx.mem_get_info().unwrap();
    (total - free) as f64 / (1 << 20) as f64
}

fn main() -> Result<()> {
    let dir = std::path::PathBuf::from(std::env::args().nth(1).expect("model dir"));
    let model = Arc::new(oominf_format::Model::open(&dir)?);
    let dims = Dims::from_config(&std::fs::read_to_string(dir.join("config.json"))?)?;
    let gpu = Gpu::new(0)?;
    let qwen = QwenModel::load(&gpu, &model, dims, None)?;
    gpu.sync()?;
    println!("after load: {:.0} MiB", used_mib(&gpu));
    let mut experts = DiskExperts::new(model.clone());
    for &t in &[64usize, 256, 512] {
        let mut state = qwen.new_state(&gpu, 4096)?;
        gpu.sync()?;
        println!("  new state: {:.0} MiB", used_mib(&gpu));
        let ids: Vec<u32> = (0..t as u32).map(|i| 1000 + i).collect();
        qwen.forward(&gpu, &ids, &mut state, &mut experts, &mut NoProbe, true)?;
        gpu.sync()?;
        println!("  prefill {t}: {:.0} MiB", used_mib(&gpu));
        for _ in 0..4 {
            qwen.forward(&gpu, &[1000], &mut state, &mut experts, &mut NoProbe, true)?;
        }
        gpu.sync()?;
        println!("  after 4 decode steps: {:.0} MiB", used_mib(&gpu));
        drop(state);
        gpu.sync()?;
        println!("  state dropped: {:.0} MiB", used_mib(&gpu));
    }
    Ok(())
}
