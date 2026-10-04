//! Full attention with the QSA sparse indexer (every 4th layer of Qwen 3.8 Flash).
//!
//! Semantics (HF `Qwen4ExpTextAttention` / `Qwen4ExpTextQSAIndexer`):
//! - `q_proj` yields `[heads, 2 * head_dim]` per token: query then output gate.
//! - q and k get a `(1 + w)` RMSNorm per head, then rotate-half RoPE on the first
//!   `head_dim * partial_rotary_factor` dims. For text the three mRoPE position
//!   components are equal, so interleaved mRoPE reduces to plain RoPE.
//! - The indexer projects `n_heads` query heads and one raw key per token. Raw keys
//!   are cached; every `ratio` consecutive keys pool (mean) into a block key, which
//!   gets a `(1 + w)` RMSNorm and RoPE at the block's first position. A query scores
//!   each complete visible block as `sum_h relu(q_h . k_b) / sqrt(head_dim)` and keeps
//!   the top `budget / ratio` blocks plus the incomplete tail.
//! - Causal GQA attention restricted to the selected keys, fp32 softmax, then
//!   `out * sigmoid(gate)` and `o_proj`.
//!
//! Precision: fp32 throughout except bf16 GEMM operands; the KV and indexer caches
//! are fp32 (no rounding we do not benefit from), at `max_tokens * (2 * kv_heads *
//! head_dim + index_head_dim) * 4` bytes per layer (about 4.6 KiB per token).

use anyhow::{Context, Result, ensure};
use oominf_cuda::{Bf16Buf, Buf, Gpu};
use oominf_format::Model;

use crate::layer::StepInput;
use crate::util::{bf16_tensor, tap};
use crate::{Dims, Probe};

#[derive(Debug, Clone)]
struct AttnDims {
    heads: usize,
    kv_heads: usize,
    head_dim: usize,
    rotary_dim: usize,
    idx_heads: usize,
    idx_kv_heads: usize,
    idx_dim: usize,
    ratio: usize,
    block_topk: usize,
}

impl AttnDims {
    fn from_text(t: &serde_json::Value) -> Result<Self> {
        let u = |k: &str| -> Result<usize> {
            t[k].as_u64()
                .map(|x| x as usize)
                .with_context(|| format!("config text_config.{k}"))
        };
        let head_dim = u("head_dim")?;
        let rope = &t["rope_parameters"];
        ensure!(
            rope["rope_type"].as_str().unwrap_or("default") == "default",
            "only default RoPE is implemented"
        );
        let prf = rope["partial_rotary_factor"]
            .as_f64()
            .or_else(|| t["partial_rotary_factor"].as_f64())
            .unwrap_or(1.0);
        let ratio = u("indexer_compress_ratio")?;
        let d = AttnDims {
            heads: u("num_attention_heads")?,
            kv_heads: u("num_key_value_heads")?,
            head_dim,
            rotary_dim: (head_dim as f64 * prf) as usize,
            idx_heads: u("indexer_n_heads")?,
            idx_kv_heads: u("indexer_kv_heads")?,
            idx_dim: u("indexer_head_dim")?,
            ratio,
            block_topk: u("indexer_budget")? / ratio,
        };
        ensure!(d.idx_kv_heads == 1, "indexer with more than one key head");
        ensure!(
            d.rotary_dim <= d.idx_dim && d.rotary_dim.is_multiple_of(2),
            "rotary dim exceeds indexer head dim"
        );
        Ok(d)
    }
}

fn rope_inv_freq(t: &serde_json::Value, rotary_dim: usize) -> Result<Vec<f32>> {
    let base = t["rope_parameters"]["rope_theta"]
        .as_f64()
        .or_else(|| t["rope_theta"].as_f64())
        .context("rope_theta")?;
    // HF: 1 / base ** (arange(0, dim, 2) / dim), in fp32.
    Ok((0..rotary_dim / 2)
        .map(|i| {
            let e = (2 * i) as f32 / rotary_dim as f32;
            (1.0 / base.powf(e as f64)) as f32
        })
        .collect())
}

pub struct Attention {
    a: AttnDims,
    q_proj: Bf16Buf,
    k_proj: Bf16Buf,
    v_proj: Bf16Buf,
    o_proj: Bf16Buf,
    q_norm: Bf16Buf,
    k_norm: Bf16Buf,
    idx_qk: Bf16Buf,
    idx_q_norm: Bf16Buf,
    idx_k_norm: Bf16Buf,
    inv_freq: Buf,
}

/// KV cache and indexer raw-key cache for one attention layer.
pub struct AttnState {
    /// `[max_tokens, kv_heads, head_dim]`, post-norm, post-RoPE.
    pub k: Buf,
    /// `[max_tokens, kv_heads, head_dim]`.
    pub v: Buf,
    /// `[max_tokens, index_head_dim]` raw indexer keys (pre-norm, pre-RoPE).
    pub idx_keys: Buf,
    pub len: usize,
    pub max_tokens: usize,
}

impl Attention {
    pub fn load(gpu: &Gpu, model: &Model, d: &Dims, layer: u32) -> Result<Self> {
        let a = AttnDims::from_text(&d.text)?;
        let p = format!("model.language_model.layers.{layer}.self_attn.");
        let w = |n: &str, s: &[u64]| bf16_tensor(gpu, model, &format!("{p}{n}"), s);
        let (h, hd) = (d.hidden as u64, a.head_dim as u64);
        let (nh, kvh) = (a.heads as u64, a.kv_heads as u64);
        let idx_out = ((a.idx_heads + a.idx_kv_heads) * a.idx_dim) as u64;
        let inv_freq = rope_inv_freq(&d.text, a.rotary_dim)?;
        Ok(Attention {
            q_proj: w("q_proj.weight", &[nh * hd * 2, h])?,
            k_proj: w("k_proj.weight", &[kvh * hd, h])?,
            v_proj: w("v_proj.weight", &[kvh * hd, h])?,
            o_proj: w("o_proj.weight", &[h, nh * hd])?,
            q_norm: w("q_norm.weight", &[hd])?,
            k_norm: w("k_norm.weight", &[hd])?,
            idx_qk: w("indexer.index_qk_proj.weight", &[idx_out, h])?,
            idx_q_norm: w("indexer.q_layernorm.weight", &[a.idx_dim as u64])?,
            idx_k_norm: w("indexer.k_layernorm.weight", &[a.idx_dim as u64])?,
            inv_freq: gpu.upload_f32(&inv_freq)?,
            a,
        })
    }

    pub fn new_state(&self, gpu: &Gpu, _d: &Dims, max_tokens: usize) -> Result<AttnState> {
        let a = &self.a;
        let kv = max_tokens * a.kv_heads * a.head_dim;
        Ok(AttnState {
            k: gpu.zeros(kv)?,
            v: gpu.zeros(kv)?,
            idx_keys: gpu.zeros(max_tokens * a.idx_dim)?,
            len: 0,
            max_tokens,
        })
    }

    /// `x` is the block input `[t, hidden]`; returns the mixer output `[t, hidden]`.
    #[allow(clippy::too_many_arguments)]
    pub fn forward(
        &self,
        gpu: &Gpu,
        d: &Dims,
        x: &Buf,
        t: usize,
        step: &StepInput,
        state: &mut AttnState,
        scratch: &mut Bf16Buf,
        probe: &mut dyn Probe,
    ) -> Result<Buf> {
        let a = &self.a;
        let h = d.hidden;
        let start = step.start_pos;
        ensure!(
            start == state.len,
            "attention step starts at {start}, cache holds {}",
            state.len
        );
        let kv_len = start + t;
        ensure!(
            kv_len <= state.max_tokens,
            "attention cache full ({kv_len} > {})",
            state.max_tokens
        );
        let (nh, kvh, hd) = (a.heads, a.kv_heads, a.head_dim);

        // Indexer: selection mask over [0, kv_len) for each query.
        let idx_w = (a.idx_heads + a.idx_kv_heads) * a.idx_dim;
        let mut idx_qk = gpu.zeros(t * idx_w)?;
        gpu.gemm_bf16(x, &self.idx_qk, &mut idx_qk, scratch, t, idx_w, h)?;
        tap(gpu, probe, "indexer.qk_proj", &mut idx_qk)?;
        let iq_w = a.idx_heads * a.idx_dim;
        let mut iq_raw = gpu.zeros(t * iq_w)?;
        gpu.copy_cols(&idx_qk, &mut iq_raw, t, idx_w, 0, iq_w)?;
        let mut ik_raw = gpu.zeros(t * a.idx_dim)?;
        gpu.copy_cols(&idx_qk, &mut ik_raw, t, idx_w, iq_w, a.idx_dim)?;
        let mut iq = gpu.zeros(t * iq_w)?;
        let rows = t * a.idx_heads;
        gpu.rmsnorm_groups(
            &iq_raw,
            &self.idx_q_norm,
            &mut iq,
            rows,
            a.idx_dim,
            a.idx_dim,
            d.eps,
            1.0,
        )?;
        tap(gpu, probe, "indexer.q_normed", &mut iq)?;
        let (rd, inv) = (a.rotary_dim, &self.inv_freq);
        gpu.rope_rotate_half(&mut iq, t, a.idx_heads, a.idx_dim, rd, inv, start, 1)?;
        tap(gpu, probe, "indexer.q_rope", &mut iq)?;
        gpu.copy_at(
            &ik_raw,
            &mut state.idx_keys,
            start * a.idx_dim,
            t * a.idx_dim,
        )?;

        let nblocks = kv_len / a.ratio;
        let mut block_keys = gpu.zeros(nblocks.max(1) * a.idx_dim)?;
        if nblocks > 0 {
            let mut pooled = gpu.zeros(nblocks * a.idx_dim)?;
            gpu.pool_rows(&state.idx_keys, &mut pooled, nblocks, a.ratio, a.idx_dim)?;
            let id = a.idx_dim;
            gpu.rmsnorm_groups(
                &pooled,
                &self.idx_k_norm,
                &mut block_keys,
                nblocks,
                id,
                id,
                d.eps,
                1.0,
            )?;
            gpu.rope_rotate_half(&mut block_keys, nblocks, 1, id, rd, inv, 0, a.ratio)?;
        }
        let score_w = kv_len / a.ratio + 1;
        let mut idx_scores = gpu.zeros(t * score_w)?;
        let mut mask = gpu.upload_bytes(&vec![0u8; t * kv_len])?;
        gpu.qsa_select(
            &iq,
            &block_keys,
            &mut idx_scores,
            &mut mask,
            t,
            start,
            a.idx_heads,
            a.idx_dim,
            a.ratio,
            a.block_topk,
            kv_len,
        )?;
        if probe.wants("indexer.num_blocks") {
            let nb = (0..t).map(|i| ((start + i + 1) / a.ratio) as f32).collect();
            probe.observe("indexer.num_blocks", nb);
        }
        if probe.wants("indexer.block_scores") {
            // [t, kv_len / ratio], zero beyond each query's visible blocks.
            let raw = gpu.download(&idx_scores)?;
            let w = kv_len / a.ratio;
            let mut padded = vec![0f32; t * w];
            for i in 0..t {
                let nb = (start + i + 1) / a.ratio;
                padded[i * w..i * w + nb].copy_from_slice(&raw[i * score_w..i * score_w + nb]);
            }
            probe.observe("indexer.block_scores", padded);
        }
        if probe.wants("indexer.mask") {
            let m = gpu.download(&mask)?;
            probe.observe("indexer.mask", m.into_iter().map(f32::from).collect());
        }
        if let Some(sub) = probe.substitute("indexer.mask") {
            ensure!(
                sub.len() == t * kv_len,
                "indexer.mask substitute has wrong length"
            );
            let bytes: Vec<u8> = sub.iter().map(|&v| u8::from(v != 0.0)).collect();
            mask = gpu.upload_bytes(&bytes)?;
        }

        // Projections, norms, RoPE.
        let mut qg = gpu.zeros(t * nh * hd * 2)?;
        gpu.gemm_bf16(x, &self.q_proj, &mut qg, scratch, t, nh * hd * 2, h)?;
        let mut k_raw = gpu.zeros(t * kvh * hd)?;
        gpu.gemm_bf16(x, &self.k_proj, &mut k_raw, scratch, t, kvh * hd, h)?;
        let mut v = gpu.zeros(t * kvh * hd)?;
        gpu.gemm_bf16(x, &self.v_proj, &mut v, scratch, t, kvh * hd, h)?;
        tap(gpu, probe, "attn.q_proj", &mut qg)?;
        tap(gpu, probe, "attn.k_proj", &mut k_raw)?;
        tap(gpu, probe, "attn.v_proj", &mut v)?;
        let mut q_raw = gpu.zeros(t * nh * hd)?;
        let mut gate = gpu.zeros(t * nh * hd)?;
        gpu.split_q_gate(&qg, &mut q_raw, &mut gate, t, nh, hd)?;
        let mut q = gpu.zeros(t * nh * hd)?;
        gpu.rmsnorm_groups(&q_raw, &self.q_norm, &mut q, t * nh, hd, hd, d.eps, 1.0)?;
        let mut k = gpu.zeros(t * kvh * hd)?;
        gpu.rmsnorm_groups(&k_raw, &self.k_norm, &mut k, t * kvh, hd, hd, d.eps, 1.0)?;
        tap(gpu, probe, "attn.q_normed", &mut q)?;
        tap(gpu, probe, "attn.k_normed", &mut k)?;
        gpu.rope_rotate_half(&mut q, t, nh, hd, rd, inv, start, 1)?;
        gpu.rope_rotate_half(&mut k, t, kvh, hd, rd, inv, start, 1)?;
        tap(gpu, probe, "attn.q_rope", &mut q)?;
        tap(gpu, probe, "attn.k_rope", &mut k)?;
        gpu.copy_at(&k, &mut state.k, start * kvh * hd, t * kvh * hd)?;
        gpu.copy_at(&v, &mut state.v, start * kvh * hd, t * kvh * hd)?;
        state.len = kv_len;

        // Masked attention, gate, output projection.
        let mut scores = gpu.zeros(t * nh * kv_len)?;
        let mut attn = gpu.zeros(t * nh * hd)?;
        gpu.attn_masked(
            &q,
            &state.k,
            &state.v,
            &mask,
            &mut scores,
            &mut attn,
            t,
            nh,
            kvh,
            hd,
            kv_len,
            kv_len,
            1.0 / (hd as f32).sqrt(),
        )?;
        tap(gpu, probe, "attn.core_out", &mut attn)?;
        gpu.mul_sigmoid(&mut attn, &gate, t * nh * hd)?;
        tap(gpu, probe, "attn.gated_out", &mut attn)?;
        let mut out = gpu.zeros(t * h)?;
        gpu.gemm_bf16(&attn, &self.o_proj, &mut out, scratch, t, h, nh * hd)?;
        self.tap_state(gpu, state, probe)?;
        Ok(out)
    }

    /// Taps the caches in the reference layout (`state.k` / `state.v` as
    /// `[kv_heads, kv_len, head_dim]`), writing substitutes back.
    fn tap_state(&self, gpu: &Gpu, state: &mut AttnState, probe: &mut dyn Probe) -> Result<()> {
        let a = &self.a;
        let (len, kvh, hd) = (state.len, a.kv_heads, a.head_dim);
        for (name, cache) in [("state.k", &mut state.k), ("state.v", &mut state.v)] {
            if !probe.wants(name) && probe.substitute(name).is_none() {
                continue;
            }
            let mut view = gpu.zeros(len * kvh * hd)?;
            gpu.swap01(cache, &mut view, len, kvh, hd)?;
            if probe.wants(name) {
                probe.observe(name, gpu.download(&view)?);
            }
            if let Some(sub) = probe.substitute(name) {
                ensure!(
                    sub.len() == view.len(),
                    "{name} substitute has wrong length"
                );
                let sub = gpu.upload_f32(&sub)?;
                let mut back = gpu.zeros(len * kvh * hd)?;
                gpu.swap01(&sub, &mut back, kvh, len, hd)?;
                gpu.copy_at(&back, cache, 0, len * kvh * hd)?;
            }
        }
        let n = len * a.idx_dim;
        let mut view = gpu.zeros(n)?;
        gpu.copy_at(&state.idx_keys, &mut view, 0, n)?;
        tap(gpu, probe, "state.indexer_k", &mut view)?;
        gpu.copy_at(&view, &mut state.idx_keys, 0, n)?;
        Ok(())
    }
}
