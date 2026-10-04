//! Weight loading and probe plumbing shared by every component.

use anyhow::{Context, Result, ensure};
use oominf_cuda::{Bf16Buf, Buf, Gpu};
use oominf_format::Model;

use crate::Probe;

/// Uploads a BF16 tensor, checking its element count against `shape`.
pub fn bf16_tensor(gpu: &Gpu, model: &Model, name: &str, shape: &[u64]) -> Result<Bf16Buf> {
    let t = model
        .tensor(name)
        .with_context(|| format!("missing tensor {name}"))?;
    ensure!(t.dtype == "BF16", "{name}: expected BF16, got {}", t.dtype);
    let want: u64 = shape.iter().product();
    ensure!(
        t.shape.iter().product::<u64>() == want,
        "{name}: shape {:?}, expected {shape:?}",
        t.shape
    );
    let bytes = model.read_tensor(t)?;
    let words: Vec<u16> = bytes
        .as_chunks::<2>()
        .0
        .iter()
        .map(|&c| u16::from_le_bytes(c))
        .collect();
    Ok(gpu.upload_u16(&words)?)
}

/// Reports `buf` to the probe as `stage` and swaps in a substitute if the probe has one.
pub fn tap(gpu: &Gpu, probe: &mut dyn Probe, stage: &str, buf: &mut Buf) -> Result<()> {
    if probe.wants(stage) {
        probe.observe(stage, gpu.download(buf)?);
    }
    if let Some(sub) = probe.substitute(stage) {
        ensure!(
            sub.len() == buf.len(),
            "{stage}: substitute has {} values, buffer {}",
            sub.len(),
            buf.len()
        );
        *buf = gpu.upload_f32(&sub)?;
    }
    Ok(())
}
