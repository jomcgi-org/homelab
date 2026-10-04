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

/// Uploads several BF16 tensors concatenated along their first (output) dimension,
/// so projections sharing one input run as a single GEMM. Each `(name, shape)` is
/// checked like [`bf16_tensor`].
pub fn bf16_concat(gpu: &Gpu, model: &Model, parts: &[(String, Vec<u64>)]) -> Result<Bf16Buf> {
    let mut words: Vec<u16> = Vec::new();
    for (name, shape) in parts {
        let t = model
            .tensor(name)
            .with_context(|| format!("missing tensor {name}"))?;
        ensure!(t.dtype == "BF16", "{name}: expected BF16, got {}", t.dtype);
        ensure!(
            t.shape.iter().product::<u64>() == shape.iter().product::<u64>(),
            "{name}: shape {:?}, expected {shape:?}",
            t.shape
        );
        let bytes = model.read_tensor(t)?;
        words.extend(
            bytes
                .as_chunks::<2>()
                .0
                .iter()
                .map(|&c| u16::from_le_bytes(c)),
        );
    }
    Ok(gpu.upload_u16(&words)?)
}

/// Taps the `[t, cols]` column range at `col` of a fused `[t, stride]` buffer as
/// `stage`: copied out only when the probe wants it, a substitute written back in
/// place. Costs nothing without a probe.
#[allow(clippy::too_many_arguments)]
pub fn tap_cols(
    gpu: &Gpu,
    probe: &mut dyn Probe,
    stage: &str,
    fused: &mut Buf,
    t: usize,
    stride: usize,
    col: usize,
    cols: usize,
) -> Result<()> {
    if probe.wants(stage) {
        let mut v = gpu.uninit(t * cols)?;
        gpu.copy_cols(fused, &mut v, t, stride, col, cols)?;
        probe.observe(stage, gpu.download(&v)?);
    }
    if let Some(sub) = probe.substitute(stage) {
        ensure!(
            sub.len() == t * cols,
            "{stage}: substitute has {} values, expected {}",
            sub.len(),
            t * cols
        );
        let s = gpu.upload_f32(&sub)?;
        gpu.put_cols(&s, fused, t, stride, col, cols)?;
    }
    Ok(())
}
