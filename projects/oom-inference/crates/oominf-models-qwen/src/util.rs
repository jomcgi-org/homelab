//! Weight loading shared by every component.

use anyhow::{Context, Result, ensure};
use oominf_core::Memory;
use oominf_format::Model;

/// Uploads a BF16 tensor, checking its element count against `shape`.
pub fn bf16_tensor<B: Memory>(
    gpu: &B,
    model: &Model,
    name: &str,
    shape: &[u64],
) -> Result<B::Bf16> {
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
    gpu.upload_bf16(&words)
}

/// Uploads several BF16 tensors concatenated along their first (output) dimension,
/// so projections sharing one input run as a single GEMM. Each `(name, shape)` is
/// checked like [`bf16_tensor`].
pub fn bf16_concat<B: Memory>(
    gpu: &B,
    model: &Model,
    parts: &[(String, Vec<u64>)],
) -> Result<B::Bf16> {
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
    gpu.upload_bf16(&words)
}
