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
//! are fp32 (no rounding we do not benefit from), about 4.6 KiB per token per layer.
//! They start small and double as the sequence grows, so short conversations leave
//! the memory to the expert tiers.

use anyhow::{Context, Result, ensure};
use oominf_cuda::{Bf16Buf, Buf, Gpu, Workspace};
use oominf_format::Model;

use crate::layer::StepInput;
use crate::util::{bf16_concat, bf16_tensor, tap};
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
    /// `index_qk_proj`, `q_proj` (query and gate), `k_proj`, `v_proj` stacked: one GEMM.
    in_proj: Bf16Buf,
    o_proj: Bf16Buf,
    q_norm: Bf16Buf,
    k_norm: Bf16Buf,
    idx_q_norm: Bf16Buf,
    idx_k_norm: Bf16Buf,
    inv_freq: Buf,
}

/// Tokens of KV cache a fresh sequence starts with; it doubles on demand up to the
/// sequence's `max_tokens`.
const INITIAL_KV_TOKENS: usize = 2048;

/// KV cache and indexer raw-key cache for one attention layer. Buffers hold `cap`
/// tokens and grow by reallocation (see [`Attention::grow`]).
pub struct AttnState {
    /// `[cap, kv_heads, head_dim]`, post-norm, post-RoPE.
    pub k: Buf,
    /// `[cap, kv_heads, head_dim]`.
    pub v: Buf,
    /// `[cap, index_head_dim]` raw indexer keys (pre-norm, pre-RoPE).
    pub idx_keys: Buf,
    /// `[cap / ratio, index_head_dim]` pooled, normed, RoPE'd block keys; the
    /// first `blocks` are valid. Blocks never change once complete, so each step only
    /// computes the ones it completes.
    pub block_keys: Buf,
    pub blocks: usize,
    pub len: usize,
    /// Tokens the buffers currently hold.
    pub cap: usize,
    /// Most tokens the sequence may ever hold.
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
            in_proj: bf16_concat(
                gpu,
                model,
                &[
                    (format!("{p}indexer.index_qk_proj.weight"), vec![idx_out, h]),
                    (format!("{p}q_proj.weight"), vec![nh * hd * 2, h]),
                    (format!("{p}k_proj.weight"), vec![kvh * hd, h]),
                    (format!("{p}v_proj.weight"), vec![kvh * hd, h]),
                ],
            )?,
            o_proj: w("o_proj.weight", &[h, nh * hd])?,
            q_norm: w("q_norm.weight", &[hd])?,
            k_norm: w("k_norm.weight", &[hd])?,
            idx_q_norm: w("indexer.q_layernorm.weight", &[a.idx_dim as u64])?,
            idx_k_norm: w("indexer.k_layernorm.weight", &[a.idx_dim as u64])?,
            inv_freq: gpu.upload_f32(&inv_freq)?,
            a,
        })
    }

    pub fn new_state(&self, gpu: &Gpu, _d: &Dims, max_tokens: usize) -> Result<AttnState> {
        let cap = self.capacity_for(INITIAL_KV_TOKENS.min(max_tokens).max(1), 0, max_tokens);
        let (kv, ik, bk) = self.buffer_lens(cap);
        Ok(AttnState {
            k: gpu.zeros(kv)?,
            v: gpu.zeros(kv)?,
            idx_keys: gpu.zeros(ik)?,
            block_keys: gpu.zeros(bk)?,
            blocks: 0,
            len: 0,
            cap,
            max_tokens,
        })
    }

    /// Element counts of the k/v, raw indexer key and block key buffers for `cap`
    /// tokens.
    fn buffer_lens(&self, cap: usize) -> (usize, usize, usize) {
        let a = &self.a;
        (
            cap * a.kv_heads * a.head_dim,
            cap * a.idx_dim,
            (cap / a.ratio).max(1) * a.idx_dim,
        )
    }

    /// Capacity to hold `tokens`: at least double `cap`, a whole number of indexer
    /// blocks, at most `max_tokens` (rounded up to a block).
    fn capacity_for(&self, tokens: usize, cap: usize, max_tokens: usize) -> usize {
        let r = self.a.ratio;
        tokens.max(2 * cap).min(max_tokens).max(tokens).div_ceil(r) * r
    }

    /// Bytes of fresh buffers that [`Attention::grow`] would allocate to hold
    /// `tokens`, or 0 when they already fit.
    pub fn growth_bytes(&self, state: &AttnState, tokens: usize) -> usize {
        if tokens <= state.cap {
            return 0;
        }
        let (kv, ik, bk) = self.buffer_lens(self.capacity_for(tokens, state.cap, state.max_tokens));
        (2 * kv + ik + bk) * std::mem::size_of::<f32>()
    }

    /// Grows the caches to hold `tokens`, keeping their contents. Kernels read the
    /// buffers by address at launch, so a reallocation between steps is invisible to
    /// them.
    pub fn grow(&self, gpu: &Gpu, state: &mut AttnState, tokens: usize) -> Result<()> {
        if tokens <= state.cap {
            return Ok(());
        }
        ensure!(
            tokens <= state.max_tokens,
            "attention cache limit is {} tokens, {tokens} requested",
            state.max_tokens
        );
        let a = &self.a;
        let cap = self.capacity_for(tokens, state.cap, state.max_tokens);
        let (kv, ik, bk) = self.buffer_lens(cap);
        let kv_w = a.kv_heads * a.head_dim;
        let moved = |old: &Buf, n: usize, len: usize| -> Result<Buf> {
            let mut new = gpu.zeros(len)?;
            if n > 0 {
                gpu.copy_range(old, 0, &mut new, 0, n)?;
            }
            Ok(new)
        };
        state.k = moved(&state.k, state.len * kv_w, kv)?;
        state.v = moved(&state.v, state.len * kv_w, kv)?;
        state.idx_keys = moved(&state.idx_keys, state.len * a.idx_dim, ik)?;
        state.block_keys = moved(&state.block_keys, state.blocks * a.idx_dim, bk)?;
        state.cap = cap;
        Ok(())
    }

    /// `x` is the block input `[t, hidden]`; returns the mixer output `[t, hidden]` as
    /// workspace buffer `attn.out` (the caller gives it back).
    #[allow(clippy::too_many_arguments)]
    pub fn forward(
        &self,
        gpu: &Gpu,
        d: &Dims,
        ws: &mut Workspace,
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
        self.grow(gpu, state, kv_len)?;
        let (nh, kvh, hd) = (a.heads, a.kv_heads, a.head_dim);
        let (rd, inv) = (a.rotary_dim, &self.inv_freq);

        // One GEMM for every projection of x: [indexer qk | q+gate | k | v].
        let idx_w = (a.idx_heads + a.idx_kv_heads) * a.idx_dim;
        let (qg_w, kv_w) = (nh * hd * 2, kvh * hd);
        let n = idx_w + qg_w + 2 * kv_w;
        let mut proj = ws.take(gpu, "attn.proj", t * n)?;
        gpu.gemm_bf16(x, &self.in_proj, &mut proj, scratch, t, n, h)?;
        let mut idx_qk = ws.take(gpu, "attn.idx_qk", t * idx_w)?;
        gpu.copy_cols(&proj, &mut idx_qk, t, n, 0, idx_w)?;
        let mut qg = ws.take(gpu, "attn.qg", t * qg_w)?;
        gpu.copy_cols(&proj, &mut qg, t, n, idx_w, qg_w)?;
        let mut k_raw = ws.take(gpu, "attn.k_raw", t * kv_w)?;
        gpu.copy_cols(&proj, &mut k_raw, t, n, idx_w + qg_w, kv_w)?;
        let mut v = ws.take(gpu, "attn.v", t * kv_w)?;
        gpu.copy_cols(&proj, &mut v, t, n, idx_w + qg_w + kv_w, kv_w)?;
        ws.give("attn.proj", proj);

        // Indexer: selection mask over [0, kv_len) for each query.
        tap(gpu, probe, "indexer.qk_proj", &mut idx_qk)?;
        let iq_w = a.idx_heads * a.idx_dim;
        let mut iq_raw = ws.take(gpu, "attn.iq_raw", t * iq_w)?;
        gpu.copy_cols(&idx_qk, &mut iq_raw, t, idx_w, 0, iq_w)?;
        let mut ik_raw = ws.take(gpu, "attn.ik_raw", t * a.idx_dim)?;
        gpu.copy_cols(&idx_qk, &mut ik_raw, t, idx_w, iq_w, a.idx_dim)?;
        ws.give("attn.idx_qk", idx_qk);
        let mut iq = ws.take(gpu, "attn.iq", t * iq_w)?;
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
        ws.give("attn.iq_raw", iq_raw);
        tap(gpu, probe, "indexer.q_normed", &mut iq)?;
        gpu.rope_rotate_half(&mut iq, t, a.idx_heads, a.idx_dim, rd, inv, start, 1)?;
        tap(gpu, probe, "indexer.q_rope", &mut iq)?;
        gpu.copy_at(
            &ik_raw,
            &mut state.idx_keys,
            start * a.idx_dim,
            t * a.idx_dim,
        )?;
        ws.give("attn.ik_raw", ik_raw);

        // Block keys for the blocks this step completes.
        let nblocks = kv_len / a.ratio;
        if nblocks > state.blocks {
            let (b0, nb_new, id) = (state.blocks, nblocks - state.blocks, a.idx_dim);
            let mut raw = ws.take(gpu, "attn.blk_raw", nb_new * a.ratio * id)?;
            gpu.copy_range(
                &state.idx_keys,
                b0 * a.ratio * id,
                &mut raw,
                0,
                nb_new * a.ratio * id,
            )?;
            let mut pooled = ws.take(gpu, "attn.blk_pooled", nb_new * id)?;
            gpu.pool_rows(&raw, &mut pooled, nb_new, a.ratio, id)?;
            ws.give("attn.blk_raw", raw);
            let mut normed = ws.take(gpu, "attn.blk_normed", nb_new * id)?;
            gpu.rmsnorm_groups(
                &pooled,
                &self.idx_k_norm,
                &mut normed,
                nb_new,
                id,
                id,
                d.eps,
                1.0,
            )?;
            ws.give("attn.blk_pooled", pooled);
            gpu.rope_rotate_half(&mut normed, nb_new, 1, id, rd, inv, b0 * a.ratio, a.ratio)?;
            gpu.copy_at(&normed, &mut state.block_keys, b0 * id, nb_new * id)?;
            ws.give("attn.blk_normed", normed);
            state.blocks = nblocks;
        }
        let score_w = kv_len / a.ratio + 1;
        let mut idx_scores = ws.take(gpu, "attn.idx_scores", t * score_w)?;
        gpu.qsa_scores(
            &iq,
            &state.block_keys,
            &mut idx_scores,
            t,
            start,
            a.idx_heads,
            a.idx_dim,
            a.ratio,
            kv_len,
        )?;
        ws.give("attn.iq", iq);
        if probe.wants("indexer.num_blocks") {
            let nb = (0..t).map(|i| ((start + i + 1) / a.ratio) as f32).collect();
            probe.observe("indexer.num_blocks", nb);
        }
        self.tap_block_scores(gpu, probe, &mut idx_scores, t, start, kv_len)?;
        // qsa_mask writes every mask byte.
        let mut mask = unsafe { gpu.stream.alloc::<u8>(t * kv_len)? };
        gpu.qsa_mask(
            &idx_scores,
            &mut mask,
            t,
            start,
            a.ratio,
            a.block_topk,
            kv_len,
        )?;
        ws.give("attn.idx_scores", idx_scores);
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

        // Norms, RoPE, cache append.
        tap(gpu, probe, "attn.q_proj", &mut qg)?;
        tap(gpu, probe, "attn.k_proj", &mut k_raw)?;
        tap(gpu, probe, "attn.v_proj", &mut v)?;
        let mut q_raw = ws.take(gpu, "attn.q_raw", t * nh * hd)?;
        let mut gate = ws.take(gpu, "attn.gate", t * nh * hd)?;
        gpu.split_q_gate(&qg, &mut q_raw, &mut gate, t, nh, hd)?;
        ws.give("attn.qg", qg);
        let mut q = ws.take(gpu, "attn.q", t * nh * hd)?;
        gpu.rmsnorm_groups(&q_raw, &self.q_norm, &mut q, t * nh, hd, hd, d.eps, 1.0)?;
        ws.give("attn.q_raw", q_raw);
        let mut k = ws.take(gpu, "attn.k", t * kv_w)?;
        gpu.rmsnorm_groups(&k_raw, &self.k_norm, &mut k, t * kvh, hd, hd, d.eps, 1.0)?;
        ws.give("attn.k_raw", k_raw);
        tap(gpu, probe, "attn.q_normed", &mut q)?;
        tap(gpu, probe, "attn.k_normed", &mut k)?;
        gpu.rope_rotate_half(&mut q, t, nh, hd, rd, inv, start, 1)?;
        gpu.rope_rotate_half(&mut k, t, kvh, hd, rd, inv, start, 1)?;
        tap(gpu, probe, "attn.q_rope", &mut q)?;
        tap(gpu, probe, "attn.k_rope", &mut k)?;
        gpu.copy_at(&k, &mut state.k, start * kv_w, t * kv_w)?;
        gpu.copy_at(&v, &mut state.v, start * kv_w, t * kv_w)?;
        ws.give("attn.k", k);
        ws.give("attn.v", v);
        state.len = kv_len;

        // Masked attention, gate, output projection.
        let mut attn = ws.take(gpu, "attn.core", t * nh * hd)?;
        gpu.attn_decode_or_masked(
            ws,
            &q,
            &state.k,
            &state.v,
            &mask,
            &mut attn,
            t,
            nh,
            kvh,
            hd,
            kv_len,
            1.0 / (hd as f32).sqrt(),
        )?;
        ws.give("attn.q", q);
        tap(gpu, probe, "attn.core_out", &mut attn)?;
        gpu.mul_sigmoid(&mut attn, &gate, t * nh * hd)?;
        ws.give("attn.gate", gate);
        tap(gpu, probe, "attn.gated_out", &mut attn)?;
        let mut out = ws.take(gpu, "attn.out", t * h)?;
        gpu.gemm_bf16(&attn, &self.o_proj, &mut out, scratch, t, h, nh * hd)?;
        ws.give("attn.core", attn);
        self.tap_state(gpu, state, probe)?;
        Ok(out)
    }

    /// Taps the block scores as `indexer.block_scores` (`[t, kv_len / ratio]`, zero
    /// beyond each query's visible blocks) and writes a substitute back, so the
    /// selection can be tested on exact reference scores.
    fn tap_block_scores(
        &self,
        gpu: &Gpu,
        probe: &mut dyn Probe,
        scores: &mut Buf,
        t: usize,
        start: usize,
        kv_len: usize,
    ) -> Result<()> {
        let stage = "indexer.block_scores";
        let ratio = self.a.ratio;
        let (w, stride) = (kv_len / ratio, kv_len / ratio + 1);
        let visible = |i: usize| (start + i + 1) / ratio;
        if probe.wants(stage) {
            let raw = gpu.download(scores)?;
            let mut padded = vec![0f32; t * w];
            for i in 0..t {
                let nb = visible(i);
                padded[i * w..i * w + nb].copy_from_slice(&raw[i * stride..i * stride + nb]);
            }
            probe.observe(stage, padded);
        }
        if let Some(sub) = probe.substitute(stage) {
            ensure!(sub.len() == t * w, "{stage} substitute has wrong length");
            let mut raw = gpu.download(scores)?;
            for i in 0..t {
                let nb = visible(i);
                raw[i * stride..i * stride + nb].copy_from_slice(&sub[i * w..i * w + nb]);
            }
            gpu.upload_into(&raw, scores)?;
        }
        Ok(())
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
        let substituted =
            probe.wants("state.indexer_k") && probe.substitute("state.indexer_k").is_some();
        tap(gpu, probe, "state.indexer_k", &mut view)?;
        gpu.copy_at(&view, &mut state.idx_keys, 0, n)?;
        if substituted {
            // Raw keys changed under the cached block keys: rebuild them next step.
            state.blocks = 0;
        }
        Ok(())
    }
}
