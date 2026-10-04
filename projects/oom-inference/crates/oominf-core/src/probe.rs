use anyhow::{Result, ensure};

use crate::backend::{DeviceBuffer, Elementwise, Memory};

/// Observes and optionally substitutes named intermediate tensors (fp32, row-major).
///
/// `observe` receives every stage a model computes when `wants` it; returning `Some`
/// from `substitute` replaces that stage's value before the model continues, which
/// lets a harness test each stage on exact reference inputs.
pub trait Probe {
    fn wants(&self, _stage: &str) -> bool {
        true
    }
    fn observe(&mut self, stage: &str, data: Vec<f32>);
    fn substitute(&mut self, _stage: &str) -> Option<Vec<f32>> {
        None
    }
}

/// A probe that observes nothing.
pub struct NoProbe;

impl Probe for NoProbe {
    fn wants(&self, _stage: &str) -> bool {
        false
    }
    fn observe(&mut self, _stage: &str, _data: Vec<f32>) {}
}

/// Reports `buf` to the probe as `stage` and swaps in a substitute if it has one.
pub fn tap<B: Memory>(b: &B, probe: &mut dyn Probe, stage: &str, buf: &mut B::F32) -> Result<()> {
    if probe.wants(stage) {
        probe.observe(stage, b.download_f32(buf)?);
    }
    if let Some(sub) = probe.substitute(stage) {
        ensure!(
            sub.len() == buf.len(),
            "{stage}: substitute has {} values, buffer {}",
            sub.len(),
            buf.len()
        );
        *buf = b.upload_f32(&sub)?;
    }
    Ok(())
}

/// Taps the `[t, cols]` column range at `col` of a fused `[t, stride]` buffer as
/// `stage`: copied out only when the probe wants it, a substitute written back in
/// place. Costs nothing without a probe.
#[allow(clippy::too_many_arguments)]
pub fn tap_cols<B: Elementwise>(
    b: &B,
    probe: &mut dyn Probe,
    stage: &str,
    fused: &mut B::F32,
    t: usize,
    stride: usize,
    col: usize,
    cols: usize,
) -> Result<()> {
    if probe.wants(stage) {
        let mut v = b.uninit(t * cols)?;
        b.copy_cols(fused, &mut v, t, stride, col, cols)?;
        probe.observe(stage, b.download_f32(&v)?);
    }
    if let Some(sub) = probe.substitute(stage) {
        ensure!(
            sub.len() == t * cols,
            "{stage}: substitute has {} values, expected {}",
            sub.len(),
            t * cols
        );
        let s = b.upload_f32(&sub)?;
        b.put_cols(&s, fused, t, stride, col, cols)?;
    }
    Ok(())
}
