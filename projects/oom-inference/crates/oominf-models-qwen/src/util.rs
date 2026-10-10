//! Weight loading shared by every component.

use anyhow::{Context, Result, ensure};
use oominf_core::{Backend, DenseFormat, Memory, Weight};

use crate::Dims;
use oominf_format::Model;

/// Uploads a BF16 tensor, checking its element count against `shape`.
pub fn bf16_tensor<B: Memory>(
    gpu: &B,
    model: &Model,
    name: &str,
    shape: &[u64],
) -> Result<B::Bf16> {
    gpu.upload_bf16(&bf16_words(model, name, shape)?)
}

/// The raw bf16 words of tensor `name`, checking its element count against `shape`.
fn bf16_words(model: &Model, name: &str, shape: &[u64]) -> Result<Vec<u16>> {
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
    Ok(bytes
        .as_chunks::<2>()
        .0
        .iter()
        .map(|&c| u16::from_le_bytes(c))
        .collect())
}

fn concat_words(model: &Model, parts: &[(String, Vec<u64>)]) -> Result<Vec<u16>> {
    let mut words: Vec<u16> = Vec::new();
    for (name, shape) in parts {
        words.extend(bf16_words(model, name, shape)?);
    }
    Ok(words)
}

/// A dense weight that stays bf16 whatever `d.dense` says: routers, whose small
/// output changes flip which experts run (a negligible share of the bytes).
pub fn exact_weight<B: Backend>(
    gpu: &B,
    model: &Model,
    name: &str,
    shape: &[u64],
) -> Result<Weight<B>> {
    Ok(Weight::Bf16(
        gpu.upload_bf16(&bf16_words(model, name, shape)?)?,
    ))
}

/// [`exact_weight`] for `parts` stacked by rows.
pub fn exact_weight_concat<B: Backend>(
    gpu: &B,
    model: &Model,
    parts: &[(String, Vec<u64>)],
) -> Result<Weight<B>> {
    Ok(Weight::Bf16(gpu.upload_bf16(&concat_words(model, parts)?)?))
}

/// A dense weight `[n, k]` loaded from bf16 `name` and stored as `d.dense`.
pub fn weight<B: Backend>(
    gpu: &B,
    model: &Model,
    name: &str,
    shape: &[u64],
    d: &Dims,
) -> Result<Weight<B>> {
    let (n, k) = rows_cols(shape)?;
    in_format(gpu, bf16_words(model, name, shape)?, n, k, d.dense)
}

/// Bf16 `parts` stacked by rows (their trailing dimensions agree) as one weight in
/// `d.dense`, so their GEMMs on the same input run as one.
pub fn weight_concat<B: Backend>(
    gpu: &B,
    model: &Model,
    parts: &[(String, Vec<u64>)],
    d: &Dims,
) -> Result<Weight<B>> {
    let mut n = 0;
    let mut k = None;
    for (_, shape) in parts {
        let (rows, cols) = rows_cols(shape)?;
        ensure!(
            k.is_none_or(|k| k == cols),
            "stacked weights differ in width"
        );
        n += rows;
        k = Some(cols);
    }
    let k = k.context("no weights to stack")?;
    in_format(gpu, concat_words(model, parts)?, n, k, d.dense)
}

fn rows_cols(shape: &[u64]) -> Result<(usize, usize)> {
    match shape {
        [n, k] => Ok((*n as usize, *k as usize)),
        other => anyhow::bail!("dense weight shape {other:?} is not 2-D"),
    }
}

/// Uploads `w` (`[n, k]` bf16 words) as `format`; FP8 is quantized on the host, so
/// no bf16 copy passes through (and fragments) device memory.
fn in_format<B: Backend>(
    gpu: &B,
    w: Vec<u16>,
    n: usize,
    k: usize,
    format: DenseFormat,
) -> Result<Weight<B>> {
    Ok(match format {
        DenseFormat::Bf16 => Weight::Bf16(gpu.upload_bf16(&w)?),
        DenseFormat::Fp8 => {
            let (q, scale) = oominf_core::fp8::quantize_blocks(&w, n, k);
            Weight::Fp8 {
                q: gpu.upload_bytes(&q)?,
                scale: gpu.upload_f32(&scale)?,
            }
        }
    })
}
