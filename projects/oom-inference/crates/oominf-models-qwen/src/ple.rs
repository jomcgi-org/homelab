//! Per-layer embedding (PLE): hashed n-gram embeddings gathered from large FP8
//! tables and added to the residual before a layer's attention hyper-connection.
//!
//! Per token, the last `ngram_size` tokens (reset at EOS boundaries) are hashed into
//! one row id per n-gram head. Rows are gathered from the FP8 tables in `tables.bin`,
//! dequantised in fp32 and concatenated into the embedding `e`. Then, per residual
//! stream `c`:
//!
//! ```text
//! gate_c = signed_sqrt(<norm_key(key_proj e)_c, norm_query(residual)_c> / sqrt(hidden))
//! v_c    = sigmoid(gate_c) * value_proj e
//! out_c  = v_c + silu(dilated_conv(norm_conv(v)_c))
//! ```
//!
//! Only the rows a step needs are read (host `pread`, then upload). A row cache or
//! asynchronous row tier can slot in behind [`Ple::gather_rows`].

use std::fs::File;
use std::os::unix::fs::FileExt;

use anyhow::{Context, Result, ensure};
use oominf_cuda::{Bf16Buf, Buf, Gpu, Workspace};
use oominf_format::{Model, TensorFile};

use crate::layer::StepInput;
use crate::util::{bf16_concat, bf16_tensor, tap};
use crate::{Dims, Probe};

pub struct Ple {
    ngram_size: usize,
    heads_per_ngram: usize,
    eos: u32,
    multipliers: Vec<i64>,
    head_sizes: Vec<i64>,
    head_offsets: Vec<i64>,
    /// Bytes of one table row (FP8, `head_dim` values).
    row_bytes: usize,
    rows_per_shard: u64,
    /// Offset of each shard in `tables.bin`, in shard order.
    shard_offsets: Vec<u64>,
    tables: File,
    scale: f32,
    /// `key_proj` then `value_proj` stacked (both read the embedding): one GEMM.
    kv_proj: Bf16Buf,
    norm_key: Bf16Buf,
    norm_query: Bf16Buf,
    norm_conv: Bf16Buf,
    conv: Bf16Buf,
    conv_kernel: usize,
    conv_dilation: usize,
    embed_dim: usize,
}

/// PLE short-conv state and n-gram token context carried across steps.
pub struct PleState {
    /// `[hc * hidden, (conv_kernel - 1) * dilation]` previous conv inputs, oldest first.
    pub conv: Buf,
    /// The last `ngram_size - 1` tokens, oldest first (EOS before any input).
    pub tokens: Vec<u32>,
}

fn i64_tensor(model: &Model, name: &str) -> Result<Vec<i64>> {
    let t = model
        .tensor(name)
        .with_context(|| format!("missing tensor {name}"))?;
    ensure!(t.dtype == "I64", "{name}: expected I64, got {}", t.dtype);
    Ok(model
        .read_tensor(t)?
        .as_chunks::<8>()
        .0
        .iter()
        .map(|&c| i64::from_le_bytes(c))
        .collect())
}

/// Hashed n-gram row ids `[t, (ngram_size - 1) * heads_per_ngram]` for `ids`, given
/// the previous `context` tokens. Matches HF `Qwen4ExpTextNGramEmbedding` exactly:
/// int64 wrapping multiply and XOR, then a non-negative remainder per head.
#[allow(clippy::too_many_arguments)]
pub fn ngram_ids(
    context: &[u32],
    ids: &[u32],
    eos: u32,
    ngram_size: usize,
    heads_per_ngram: usize,
    multipliers: &[i64],
    head_sizes: &[i64],
    head_offsets: &[i64],
) -> Vec<i64> {
    let hist: Vec<u32> = context.iter().chain(ids).copied().collect();
    let heads = (ngram_size - 1) * heads_per_ngram;
    let mut out = Vec::with_capacity(ids.len() * heads);
    // Index of the last EOS strictly before the current position.
    let mut prev_eos: i64 = -1;
    for (i, &tok) in hist.iter().enumerate() {
        if i >= context.len() {
            let pos_in_seg = i as i64 - (prev_eos + 1);
            let shifted = |s: usize| -> i64 {
                if pos_in_seg >= s as i64 && i >= s {
                    hist[i - s] as i64
                } else {
                    eos as i64
                }
            };
            for n in 2..=ngram_size {
                let mut mixed = shifted(0).wrapping_mul(multipliers[0]);
                for (p, &m) in multipliers.iter().enumerate().take(n).skip(1) {
                    mixed ^= shifted(p).wrapping_mul(m);
                }
                let first = (n - 2) * heads_per_ngram;
                for h in first..first + heads_per_ngram {
                    out.push(mixed.rem_euclid(head_sizes[h]) + head_offsets[h]);
                }
            }
        }
        if tok == eos {
            prev_eos = i as i64;
        }
    }
    out
}

impl Ple {
    pub fn load(gpu: &Gpu, model: &Model, d: &Dims, layer: u32) -> Result<Self> {
        let p = format!("model.language_model.layers.{layer}.ple.");
        let u = |k: &str| -> Result<usize> {
            d.text[k]
                .as_u64()
                .map(|v| v as usize)
                .with_context(|| format!("config text_config.{k}"))
        };
        let ngram_size = u("ngram_size")?;
        let heads_per_ngram = u("heads_per_ngram")?;
        let embed_dim = u("ple_embed_dim")?;
        let conv_kernel = u("ple_conv_kernel_size")?;
        let eos = match &d.text["eos_token_id"] {
            serde_json::Value::Array(a) => a.first().and_then(|v| v.as_u64()),
            v => v.as_u64(),
        }
        .context("config text_config.eos_token_id")? as u32;
        let heads = (ngram_size - 1) * heads_per_ngram;
        ensure!(
            embed_dim % heads == 0,
            "ple_embed_dim not divisible by heads"
        );

        let e = format!("{p}ple_embedding.");
        let multipliers = i64_tensor(model, &format!("{e}layer_multipliers"))?;
        let head_sizes = i64_tensor(model, &format!("{e}ngram_heads_vocab_sizes"))?;
        let head_offsets = i64_tensor(model, &format!("{e}ngram_heads_offsets"))?;
        ensure!(
            multipliers.len() == ngram_size
                && head_sizes.len() == heads
                && head_offsets.len() == heads,
            "PLE hash tensors do not match config"
        );

        // Table shards, in shard order.
        let shard_prefix = format!("{e}ngram_embedding.shard_");
        let mut shards: Vec<(u64, &oominf_format::TensorEntry)> = model
            .index()
            .tensors
            .iter()
            .filter_map(|t| {
                let n = t
                    .name
                    .strip_prefix(&shard_prefix)?
                    .strip_suffix(".weight")?;
                Some((n.parse().ok()?, t))
            })
            .collect();
        shards.sort_by_key(|(n, _)| *n);
        ensure!(!shards.is_empty(), "no PLE table shards for layer {layer}");
        let head_dim = embed_dim / heads;
        for (i, (n, t)) in shards.iter().enumerate() {
            ensure!(*n == i as u64, "PLE shard {i} missing");
            ensure!(
                t.file == TensorFile::Tables
                    && t.dtype == "F8_E4M3"
                    && t.shape == [shards[0].1.shape[0], head_dim as u64],
                "PLE shard {i}: unexpected {} {:?}",
                t.dtype,
                t.shape
            );
        }
        let rows_per_shard = shards[0].1.shape[0];
        let shard_offsets = shards.iter().map(|(_, t)| t.offset).collect();

        let scale_t = model
            .tensor(&format!("{e}ngram_embedding.weight_scale"))
            .context("missing PLE weight_scale")?;
        let raw = model.read_tensor(scale_t)?;
        let scale = match scale_t.dtype.as_str() {
            "BF16" => f32::from_bits((u16::from_le_bytes([raw[0], raw[1]]) as u32) << 16),
            "F32" => f32::from_le_bytes(raw[..4].try_into().unwrap()),
            other => anyhow::bail!("PLE weight_scale dtype {other}"),
        };

        let tables_path = model.dir().join(TensorFile::Tables.file_name());
        let tables =
            File::open(&tables_path).with_context(|| format!("open {}", tables_path.display()))?;
        let (h, r, ed) = (d.hidden as u64, d.residual() as u64, embed_dim as u64);
        let w = |n: &str, s: &[u64]| bf16_tensor(gpu, model, &format!("{p}{n}"), s);
        Ok(Ple {
            ngram_size,
            heads_per_ngram,
            eos,
            multipliers,
            head_sizes,
            head_offsets,
            row_bytes: head_dim,
            rows_per_shard,
            shard_offsets,
            tables,
            scale,
            kv_proj: bf16_concat(
                gpu,
                model,
                &[
                    (format!("{p}key_proj.weight"), vec![r, ed]),
                    (format!("{p}value_proj.weight"), vec![h, ed]),
                ],
            )?,
            norm_key: w("norm_key.weight", &[r])?,
            norm_query: w("norm_query.weight", &[r])?,
            norm_conv: w("norm_conv.weight", &[r])?,
            conv: w("conv1d.weight", &[r, conv_kernel as u64])?,
            conv_kernel,
            // HF uses the n-gram size as the short conv's dilation.
            conv_dilation: ngram_size,
            embed_dim,
        })
    }

    pub fn new_state(&self, gpu: &Gpu, d: &Dims) -> Result<PleState> {
        Ok(PleState {
            conv: gpu.zeros(d.residual() * (self.conv_kernel - 1) * self.conv_dilation)?,
            tokens: vec![self.eos; self.ngram_size - 1],
        })
    }

    /// Reads the FP8 rows for `ids` (concatenated-table row numbers) into one
    /// contiguous host buffer, row after row.
    fn gather_rows(&self, ids: &[i64]) -> Result<Vec<u8>> {
        let mut out = vec![0u8; ids.len() * self.row_bytes];
        for (i, &id) in ids.iter().enumerate() {
            ensure!(id >= 0, "negative PLE row id {id}");
            let (shard, row) = (
                id as u64 / self.rows_per_shard,
                id as u64 % self.rows_per_shard,
            );
            let base = *self
                .shard_offsets
                .get(shard as usize)
                .with_context(|| format!("PLE row {id} beyond the table"))?;
            self.tables.read_exact_at(
                &mut out[i * self.row_bytes..(i + 1) * self.row_bytes],
                base + row * self.row_bytes as u64,
            )?;
        }
        Ok(out)
    }

    /// Returns the PLE contribution `[t, hc * hidden]` to add to `residual`, as
    /// workspace buffer `ple.out` (the caller gives it back).
    #[allow(clippy::too_many_arguments)]
    pub fn forward(
        &self,
        gpu: &Gpu,
        d: &Dims,
        ws: &mut Workspace,
        residual: &Buf,
        t: usize,
        step: &StepInput,
        state: &mut PleState,
        scratch: &mut Bf16Buf,
        probe: &mut dyn Probe,
    ) -> Result<Buf> {
        ensure!(step.token_ids.len() == t, "PLE needs one token id per row");
        let (h, r, ed) = (d.hidden, d.residual(), self.embed_dim);

        let ids = ngram_ids(
            &state.tokens,
            step.token_ids,
            self.eos,
            self.ngram_size,
            self.heads_per_ngram,
            &self.multipliers,
            &self.head_sizes,
            &self.head_offsets,
        );
        // Row ids exceed f32's exact range, so they are reported but never substituted.
        if probe.wants("ple.ngram_ids") {
            probe.observe("ple.ngram_ids", ids.iter().map(|&v| v as f32).collect());
        }
        let ctx = state.tokens.len();
        let hist: Vec<u32> = state.tokens.iter().chain(step.token_ids).copied().collect();
        state.tokens = hist[hist.len() - ctx..].to_vec();

        let rows = gpu.upload_bytes(&self.gather_rows(&ids)?)?;
        let mut emb = ws.take(gpu, "ple.emb", t * ed)?;
        gpu.fp8_dequant_scaled(&rows, self.scale, &mut emb, t * ed)?;
        tap(gpu, probe, "ple.ngram_embed", &mut emb)?;

        let mut kv = ws.take(gpu, "ple.kv", t * (r + h))?;
        gpu.gemm_bf16(&emb, &self.kv_proj, &mut kv, scratch, t, r + h, ed)?;
        ws.give("ple.emb", emb);
        let mut key = ws.take(gpu, "ple.key", t * r)?;
        gpu.copy_cols(&kv, &mut key, t, r + h, 0, r)?;
        let mut value = ws.take(gpu, "ple.value", t * h)?;
        gpu.copy_cols(&kv, &mut value, t, r + h, r, h)?;
        ws.give("ple.kv", kv);
        let mut key_n = ws.take(gpu, "ple.key_n", t * r)?;
        gpu.rmsnorm_groups(&key, &self.norm_key, &mut key_n, t, r, h, d.eps, 1.0)?;
        ws.give("ple.key", key);
        let mut query_n = ws.take(gpu, "ple.query_n", t * r)?;
        gpu.rmsnorm_groups(
            residual,
            &self.norm_query,
            &mut query_n,
            t,
            r,
            h,
            d.eps,
            1.0,
        )?;
        let mut gated = ws.take(gpu, "ple.gated", t * r)?;
        gpu.ple_gate(&key_n, &query_n, &value, &mut gated, t, d.hc, h)?;
        ws.give("ple.key_n", key_n);
        ws.give("ple.query_n", query_n);
        ws.give("ple.value", value);
        let mut gated_n = ws.take(gpu, "ple.gated_n", t * r)?;
        gpu.rmsnorm_groups(&gated, &self.norm_conv, &mut gated_n, t, r, h, d.eps, 1.0)?;
        let mut out = ws.take(gpu, "ple.out", t * r)?;
        gpu.dilated_conv_silu_add(
            &gated_n,
            &mut state.conv,
            &self.conv,
            &gated,
            &mut out,
            t,
            r,
            self.conv_kernel,
            self.conv_dilation,
        )?;
        ws.give("ple.gated", gated);
        ws.give("ple.gated_n", gated_n);

        tap(gpu, probe, "state.ple_conv", &mut state.conv)?;
        if probe.wants("state.ple_tokens") {
            probe.observe(
                "state.ple_tokens",
                state.tokens.iter().map(|&v| v as f32).collect(),
            );
        }
        if let Some(sub) = probe.substitute("state.ple_tokens") {
            ensure!(
                sub.len() == ctx,
                "state.ple_tokens substitute has wrong length"
            );
            state.tokens = sub.iter().map(|&v| v as u32).collect();
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn eos_resets_the_ngram_context() {
        let (eos, mult, sizes, offs) = (9, [3i64, 5, 7], [1000i64, 1000], [0i64, 1000]);
        // Previous context [eos, eos]: every shifted token is eos.
        let ids = ngram_ids(
            &[eos, eos],
            &[1, 2, eos, 4],
            eos,
            3,
            1,
            &mult,
            &sizes,
            &offs,
        );
        let h = |a: i64, b: i64, c: Option<i64>| {
            let m = (a * 3) ^ (b * 5);
            let m3 = c.map_or(m, |c| m ^ (c * 7));
            (m.rem_euclid(1000), m3.rem_euclid(1000) + 1000)
        };
        let tok = |t: usize| -> Vec<i64> {
            let (a, b) = match t {
                0 => h(1, 9, Some(9)),
                1 => h(2, 1, Some(9)),
                2 => h(9, 2, Some(1)),
                // After an EOS, history before it is replaced with eos.
                _ => h(4, 9, Some(9)),
            };
            vec![a, b]
        };
        let want: Vec<i64> = (0..4).flat_map(tok).collect();
        assert_eq!(ids, want);
    }

    #[test]
    fn remainder_is_non_negative_after_wrapping() {
        let big = i64::MAX / 3;
        let ids = ngram_ids(
            &[1, 1],
            &[u32::MAX],
            0,
            3,
            1,
            &[big, big, big],
            &[97, 101],
            &[0, 97],
        );
        assert!(ids.iter().all(|&v| v >= 0));
    }
}
