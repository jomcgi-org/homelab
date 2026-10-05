//! The whole text model: embedding, decoder layers, final hyper-connection mixer
//! and `lm_head`.

use std::cell::RefCell;
use std::rc::Rc;

use anyhow::{Context, Result, ensure};
use oominf_core::{Backend, DeviceBuffer, ExpertSource, Fetched, NoProbe, Probe, Workspace, tap};
use oominf_format::Model;

use crate::Dims;
use crate::hc::HyperConn;
use crate::layer::{DecoderLayer, LayerState, StepInput};
use crate::util::bf16_tensor;

/// Prompt tokens per prefill chunk by default: bounds prefill workspace VRAM.
pub const PREFILL_CHUNK: usize = 512;

/// Longest stretch of a prompt prefilled layer by layer at once. Residuals of every
/// token in it live across layers (about 40 KB per token), so longer prompts, or
/// prompts whose residuals do not fit beside their cache, run window by window.
/// Each window sweeps every layer's experts through VRAM again (about 4.5 s for
/// Qwen 3.8 Flash on a 4090), so windows are as long as memory allows.
pub const PREFILL_WINDOW_TOKENS: usize = 128 * 1024;

/// Windows are not split below this for lack of memory.
const PREFILL_MIN_WINDOW_TOKENS: usize = 8 * 1024;

/// Workspace buffers sized for prefill (fetch groups, sequence-length step buffers,
/// fp32 KV shadows) that decode steps do not need.
const PREFILL_ONLY_BUFFERS: &[&str] = &[
    "attn.shadow_k",
    "attn.shadow_v",
    "attn.shadow_rows",
    "attn.mask",
    "attn.idx_scores",
    "attn.sel_prefill",
    "moe.h",
    "moe.y",
    "moe.g",
    "moe.u",
    "layer.moe_in",
    "layer.moe_part",
];

/// Most prompt tokens whose routed experts one prefill fetch loads and runs as one
/// step: enough assignments per expert for tensor-core tiles, bounded so the
/// group's activations at the MoE fit beside the expert tiers.
pub const PREFILL_FETCH_TOKENS: usize = 2048;

pub struct QwenModel<B: Backend> {
    pub dims: Dims,
    vocab: usize,
    /// `embed_tokens` kept on the host as bf16 words; a step gathers its rows.
    embed: Vec<u16>,
    layers: Vec<DecoderLayer<B>>,
    final_mixer: HyperConn<B>,
    lm_head: B::Bf16,
    /// The multi-token-prediction head, when the checkpoint has one.
    mtp: Option<Mtp<B>>,
}

/// The checkpoint's multi-token-prediction head. Given the final residual at
/// position `i` (before the final mixer) and the token at `i + 1`, it predicts the
/// token at `i + 2`: both inputs are normalised and projected, the token's
/// projection is added to every residual stream, one full-attention decoder layer
/// runs, and the head's own mixer and the shared `lm_head` produce logits. A chain
/// of drafts feeds each prediction back with the layer's output residual.
struct Mtp<B: Backend> {
    layer: DecoderLayer<B>,
    norm_embedding: B::Bf16,
    norm_hidden: B::Bf16,
    fc_embedding: B::Bf16,
    fc_hidden: B::Bf16,
    mixer: HyperConn<B>,
    /// `[hc]` ones: adds one row to every stream through the combine kernel.
    ones: B::F32,
}

/// Longest draft chain a sequence supports.
pub const MAX_DRAFT: usize = 8;

/// Device memory a step's workspace may need on top of what it already holds (a
/// prefill chunk's activations and MoE scratch), kept free when caches grow.
/// Expert tiers should leave at least this much when they size themselves.
pub const WORKSPACE_HEADROOM: usize = 3 << 29;

/// Free device memory left beyond the headroom when expert tiers take memory back,
/// so a sequence's first KV growths do not immediately take it away again.
const RECLAIM_SLACK: usize = 512 << 20;

/// Everything carried across steps of one sequence.
pub struct SeqState<B: Backend> {
    pub layers: Vec<LayerState<B>>,
    /// Tokens processed so far (the next step's start position).
    pub pos: usize,
    /// Scratch buffers shared by every layer.
    pub ws: Rc<RefCell<Workspace<B>>>,
    /// With an MTP head: the final residual rows of the last step, and which row is
    /// the sequence's last token (the draft head's input).
    pub(crate) hidden: Option<B::F32>,
    pub(crate) hidden_row: usize,
    /// The MTP layer's attention state (its draft chain).
    mtp: Option<LayerState<B>>,
    /// Start and length of the last step if it can be rewound.
    pub(crate) rewindable: Option<(usize, usize)>,
}

/// Renames a layer's stages to `{stage}.{layer}` for a model-level probe.
struct LayerProbe<'a> {
    inner: &'a mut dyn Probe,
    layer: u32,
}

impl Probe for LayerProbe<'_> {
    fn wants(&self, stage: &str) -> bool {
        self.inner.wants(&format!("{stage}.{}", self.layer))
    }
    fn observe(&mut self, stage: &str, data: Vec<f32>) {
        self.inner.observe(&format!("{stage}.{}", self.layer), data);
    }
    fn substitute(&mut self, stage: &str) -> Option<Vec<f32>> {
        self.inner.substitute(&format!("{stage}.{}", self.layer))
    }
}

fn bf16_to_f32(w: u16) -> f32 {
    f32::from_bits((w as u32) << 16)
}

impl<B: Backend> QwenModel<B> {
    /// Lets decode-sized steps compute host-resident experts on `pool`, in every
    /// layer and the draft head.
    pub fn set_host_experts(&mut self, pool: std::sync::Arc<oominf_cpu::HostExperts>) {
        for l in &mut self.layers {
            l.set_host_experts(pool.clone());
        }
        if let Some(m) = &mut self.mtp {
            m.layer.set_host_experts(pool);
        }
    }

    /// Loads every layer's dense weights onto the GPU (experts stay on disk and come
    /// through an [`ExpertSource`]). `layers` limits how many decoder layers load.
    pub fn load(gpu: &B, model: &Model, dims: Dims, layers: Option<usize>) -> Result<Self> {
        let vocab = dims.text["vocab_size"]
            .as_u64()
            .context("config text_config.vocab_size")? as usize;
        let h = dims.hidden as u64;
        let embed_t = model
            .tensor("model.language_model.embed_tokens.weight")
            .context("missing embed_tokens")?;
        ensure!(
            embed_t.dtype == "BF16" && embed_t.shape == [vocab as u64, h],
            "embed_tokens is {} {:?}",
            embed_t.dtype,
            embed_t.shape
        );
        let embed = model
            .read_tensor(embed_t)?
            .as_chunks::<2>()
            .0
            .iter()
            .map(|&c| u16::from_le_bytes(c))
            .collect();
        let n = layers.unwrap_or(dims.layer_kinds.len());
        let layers = (0..n as u32)
            .map(|l| DecoderLayer::load(gpu, model, &dims, l))
            .collect::<Result<Vec<_>>>()?;
        let mtp_group = dims.layer_kinds.len() as u32;
        let mtp = if n == dims.layer_kinds.len()
            && model.tensor("mtp.fc_embedding.weight").is_some()
            && model.expert_group(mtp_group).is_some()
        {
            let (hu, ru) = (h, dims.residual() as u64);
            Some(Mtp {
                layer: DecoderLayer::load_mtp(gpu, model, &dims, mtp_group)?,
                norm_embedding: bf16_tensor(gpu, model, "mtp.pre_fc_norm_embedding.weight", &[hu])?,
                norm_hidden: bf16_tensor(gpu, model, "mtp.pre_fc_norm_hidden.weight", &[ru])?,
                fc_embedding: bf16_tensor(gpu, model, "mtp.fc_embedding.weight", &[hu, hu])?,
                fc_hidden: bf16_tensor(gpu, model, "mtp.fc_hidden.weight", &[hu, hu])?,
                mixer: HyperConn::load(gpu, model, &dims, "mtp.hyper_connection_mixer", false)?,
                ones: gpu.upload_f32(&vec![1.0; dims.hc])?,
            })
        } else {
            None
        };
        Ok(QwenModel {
            mtp,
            final_mixer: HyperConn::load(
                gpu,
                model,
                &dims,
                "model.language_model.hyper_connection_mixer",
                false,
            )?,
            lm_head: bf16_tensor(gpu, model, "lm_head.weight", &[vocab as u64, h])?,
            vocab,
            embed,
            layers,
            dims,
        })
    }

    pub fn vocab(&self) -> usize {
        self.vocab
    }

    pub fn new_state(&self, gpu: &B, max_tokens: usize) -> Result<SeqState<B>> {
        let ws = Rc::new(RefCell::new(Workspace::new()));
        Ok(SeqState {
            layers: self
                .layers
                .iter()
                .map(|l| l.new_state_shared(gpu, &self.dims, max_tokens, ws.clone()))
                .collect::<Result<_>>()?,
            pos: 0,
            hidden: None,
            hidden_row: 0,
            mtp: match &self.mtp {
                Some(m) => {
                    Some(
                        m.layer
                            .new_state_shared(gpu, &self.dims, MAX_DRAFT, ws.clone())?,
                    )
                }
                None => None,
            },
            rewindable: None,
            ws,
        })
    }

    /// The decoder layers, in order.
    pub(crate) fn layers(&self) -> &[DecoderLayer<B>] {
        &self.layers
    }

    /// Whether the model can propose draft tokens ([`Self::draft`]).
    pub fn has_draft(&self) -> bool {
        self.mtp.is_some()
    }

    /// Keeps rows `[first, first + rows)` of the residual `x` as the draft head's
    /// input, the last of them being the sequence's last token.
    fn keep_hidden(
        &self,
        gpu: &B,
        state: &mut SeqState<B>,
        x: &B::F32,
        first: usize,
        rows: usize,
    ) -> Result<()> {
        if self.mtp.is_none() {
            return Ok(());
        }
        let r = self.dims.residual();
        let buf = match state.hidden.take() {
            Some(b) if b.len() >= rows * r => b,
            _ => gpu.uninit(rows.max(MAX_DRAFT + 1) * r)?,
        };
        let mut buf = buf;
        gpu.copy_range(x, first * r, &mut buf, 0, rows * r)?;
        state.hidden = Some(buf);
        state.hidden_row = rows - 1;
        Ok(())
    }

    /// Embeds `token_ids` (repeated over the hc streams) into `x`.
    fn embed_into(&self, gpu: &B, token_ids: &[u32], x: &mut B::F32) -> Result<()> {
        let d = &self.dims;
        let h = d.hidden;
        let mut host = Vec::with_capacity(token_ids.len() * d.residual());
        for &id in token_ids {
            ensure!((id as usize) < self.vocab, "token id {id} out of vocab");
            let row = &self.embed[id as usize * h..(id as usize + 1) * h];
            for _ in 0..d.hc {
                host.extend(row.iter().map(|&w| bf16_to_f32(w)));
            }
        }
        gpu.upload_into(&host, x)?;
        Ok(())
    }

    /// Final hyper-connection mixer and `lm_head` over the residual `x` of `t`
    /// tokens: logits `[t, vocab]`, or only the last row with `last_only`.
    #[allow(clippy::too_many_arguments)]
    fn head(
        &self,
        gpu: &B,
        ws: &mut Workspace<B>,
        x: &B::F32,
        t: usize,
        probe: &mut dyn Probe,
        last_only: bool,
    ) -> Result<B::F32> {
        let d = &self.dims;
        let h = d.hidden;
        let mut scratch = ws.take_bf16(gpu, "gemm.scratch", t * d.residual())?;
        let (mut mixed, _) =
            self.final_mixer
                .mix(gpu, d, ws, x, t, &mut scratch, probe, "final_hc")?;
        tap(gpu, probe, "mixer_out", &mut mixed)?;
        let rows = if last_only { 1 } else { t };
        let input = if last_only && t > 1 {
            let mut last = ws.take(gpu, "model.last", h)?;
            gpu.copy_rows(&mixed, t - 1, 1, h, &mut last)?;
            ws.give("hc.mixed", mixed);
            last
        } else {
            mixed
        };
        let mut logits = gpu.uninit(rows * self.vocab)?;
        gpu.gemm_bf16(
            &input,
            &self.lm_head,
            &mut logits,
            &mut scratch,
            rows,
            self.vocab,
            h,
        )?;
        ws.give_bf16("gemm.scratch", scratch);
        ws.give(
            if last_only && t > 1 {
                "model.last"
            } else {
                "hc.mixed"
            },
            input,
        );
        if !last_only {
            tap(gpu, probe, "logits", &mut logits)?;
        }
        Ok(logits)
    }

    /// The largest sequence-length-dependent step buffers any layer allocates for a
    /// step of `t` tokens over `kv_len` (layers run one at a time).
    fn step_bytes(&self, t: usize, kv_len: usize) -> usize {
        self.layers
            .iter()
            .map(|l| l.step_bytes(t, kv_len))
            .max()
            .unwrap_or(0)
    }

    /// Makes the KV caches hold `tokens`. When the growth plus `transient` bytes
    /// (other allocations the step is about to make) plus workspace headroom would
    /// not fit in free device memory, asks `experts` to give memory back first.
    pub fn reserve_kv(
        &self,
        gpu: &B,
        state: &mut SeqState<B>,
        tokens: usize,
        transient: usize,
        experts: &mut dyn ExpertSource<B>,
    ) -> Result<()> {
        let need = self.kv_growth_bytes(state, tokens);
        if need == 0 && transient == 0 {
            return Ok(());
        }
        gpu.sync()?;
        let (free, _) = gpu.mem_info()?;
        let headroom = WORKSPACE_HEADROOM.saturating_sub(state.ws.borrow().bytes());
        let want = need + transient + headroom;
        if free < want {
            experts.release_vram(gpu, want - free)?;
        }
        for (l, s) in self.layers.iter().zip(state.layers.iter_mut()) {
            l.grow_kv(gpu, s, tokens)?;
        }
        Ok(())
    }

    /// Device memory the caches of `state` need to grow to `tokens`.
    fn kv_growth_bytes(&self, state: &SeqState<B>, tokens: usize) -> usize {
        self.layers
            .iter()
            .zip(&state.layers)
            .map(|(l, s)| l.kv_growth_bytes(s, tokens))
            .sum()
    }

    /// Offers `experts` the device memory a fresh `state` does not need (free memory
    /// beyond the workspace headroom and some slack), e.g. after a long sequence
    /// was dropped.
    pub fn reclaim_vram(
        &self,
        gpu: &B,
        state: &SeqState<B>,
        experts: &mut dyn ExpertSource<B>,
    ) -> Result<()> {
        gpu.sync()?;
        let (free, _) = gpu.mem_info()?;
        let keep = WORKSPACE_HEADROOM.saturating_sub(state.ws.borrow().bytes()) + RECLAIM_SLACK;
        if free > keep {
            experts.reclaim_vram(gpu, free - keep)?;
        }
        Ok(())
    }

    /// Runs `token_ids` through the model as one step and returns logits `[t, vocab]`
    /// (or only the last row with `last_only`). Stages are tapped as
    /// `{stage}.{layer}` plus `residual_in`, `mixer_out` and `logits`.
    pub fn forward(
        &self,
        gpu: &B,
        token_ids: &[u32],
        state: &mut SeqState<B>,
        experts: &mut dyn ExpertSource<B>,
        probe: &mut dyn Probe,
        last_only: bool,
    ) -> Result<B::F32> {
        self.step(gpu, token_ids, state, experts, probe, last_only, false)
    }

    /// [`Self::forward`]; with `checkpoint` the step can afterwards be cut back to
    /// any of its rows with [`Self::rewind`].
    #[allow(clippy::too_many_arguments)]
    pub fn step(
        &self,
        gpu: &B,
        token_ids: &[u32],
        state: &mut SeqState<B>,
        experts: &mut dyn ExpertSource<B>,
        probe: &mut dyn Probe,
        last_only: bool,
        checkpoint: bool,
    ) -> Result<B::F32> {
        let d = &self.dims;
        let t = token_ids.len();
        ensure!(t > 0, "empty step");
        self.reserve_kv(gpu, state, state.pos + t, 0, experts)?;
        let mut x = state
            .ws
            .borrow_mut()
            .take(gpu, "model.embed", t * d.residual())?;
        self.embed_into(gpu, token_ids, &mut x)?;
        tap(gpu, probe, "residual_in", &mut x)?;
        // Name the current residual is given back under once a layer has consumed it;
        // layer outputs then alternate between two stable "layer.out" buffers.
        let mut x_name = "model.embed";

        let step = StepInput {
            token_ids,
            start_pos: state.pos,
            checkpoint,
        };
        state.rewindable = checkpoint.then_some((state.pos, t));
        for (layer, st) in self.layers.iter().zip(state.layers.iter_mut()) {
            let mut lp = LayerProbe {
                inner: probe,
                layer: layer.layer,
            };
            let next = layer.forward(gpu, d, &x, t, &step, st, experts, &mut lp)?;
            // Hand the previous residual back so the next layer reuses it.
            state
                .ws
                .borrow_mut()
                .give(x_name, std::mem::replace(&mut x, next));
            x_name = "layer.out";
        }
        state.pos += t;
        self.keep_hidden(gpu, state, &x, 0, t)?;

        let mut ws = state.ws.borrow_mut();
        let logits = self.head(gpu, &mut ws, &x, t, probe, last_only)?;
        ws.give(x_name, x);
        Ok(logits)
    }

    /// Prefills a prompt layer by layer: every `chunk`-token slice of `token_ids`
    /// runs through layer 0, then through layer 1, and so on. Within a layer the
    /// chunks run in order with that layer's state carried, so the result matches
    /// running the chunks one after another through the whole model up to
    /// floating-point summation order (grouped routing and expert steps change GEMM
    /// shapes, not the computation).
    ///
    /// A prompt of more than one chunk: each layer runs the token mixer of up to
    /// [`PREFILL_FETCH_TOKENS`] tokens of chunks, routes them, and fetches the union
    /// of their experts once; it then predicts the next layer's experts from the
    /// same MoE inputs and has the source stage them while this layer's experts
    /// compute. A prompt longer than [`PREFILL_WINDOW_TOKENS`] runs window by
    /// window (each window through every layer), so the residuals held across
    /// layers are bounded by the window. Returns the last row of logits, or `None`
    /// if `cancelled()` turned true (checked between layers); the state is then
    /// partially advanced and must be discarded.
    #[allow(clippy::too_many_arguments)]
    pub fn prefill(
        &self,
        gpu: &B,
        token_ids: &[u32],
        chunk: usize,
        state: &mut SeqState<B>,
        experts: &mut dyn ExpertSource<B>,
        cancelled: &dyn Fn() -> bool,
    ) -> Result<Option<B::F32>> {
        ensure!(!token_ids.is_empty(), "empty prompt");
        // The fewest equal windows (each costs a sweep through every layer's
        // experts) whose buffers fit beside the whole prompt's cache.
        let end = state.pos + token_ids.len();
        gpu.sync()?;
        let (free, _) = gpu.mem_info()?;
        let room = (free + experts.releasable_vram())
            .saturating_sub(self.kv_growth_bytes(state, end))
            .saturating_sub(WORKSPACE_HEADROOM.saturating_sub(state.ws.borrow().bytes()))
            .saturating_sub(RECLAIM_SLACK);
        let mut n = token_ids.len().div_ceil(PREFILL_WINDOW_TOKENS);
        while token_ids.len().div_ceil(n) > PREFILL_MIN_WINDOW_TOKENS
            && self.prefill_bytes(token_ids.len().div_ceil(n), end, chunk) > room
        {
            n += 1;
        }
        let windows: Vec<&[u32]> = token_ids.chunks(token_ids.len().div_ceil(n)).collect();
        // Every window but the last reserves the whole prompt's cache and keeps the
        // prefill buffers, so the expert tier is not shrunk and regrown per window.
        for w in &windows[..windows.len() - 1] {
            if self
                .prefill_window(gpu, w, chunk, state, experts, cancelled, Some(end))?
                .is_none()
            {
                return Ok(None);
            }
        }
        let last = windows[windows.len() - 1];
        Ok(self
            .prefill_window(gpu, last, chunk, state, experts, cancelled, None)?
            .flatten())
    }

    /// Device memory a layer-major prefill of `t` tokens ending at `total` holds
    /// beyond the caches. Residuals of every chunk live across layers (about 40 KB
    /// per token); one fetch group's activations at the MoE live until it computes,
    /// and its routed experts run as one step (`top_k` rows of hidden + 3 x inter
    /// each); attention buffers and one layer's fp32 KV shadow.
    fn prefill_bytes(&self, t: usize, total: usize, chunk: usize) -> usize {
        let d = &self.dims;
        let r = d.residual();
        let group = t.min(PREFILL_FETCH_TOKENS);
        let moe = d.top_k * (d.hidden + 3 * d.moe_inter) + 2 * d.hidden;
        let held = group * (2 * r + d.hidden + moe);
        let attn = self.step_bytes(chunk.min(t), total);
        let shadow = self
            .layers
            .iter()
            .map(|l| l.shadow_bytes(total))
            .max()
            .unwrap_or(0);
        (t * r + held) * std::mem::size_of::<f32>() + attn + shadow
    }

    /// [`Self::prefill`] of at most one window: `None` if cancelled, else the
    /// window's last row of logits. With `more`, the end of the whole prompt, the
    /// window is not the last: it reserves the cache up to `more`, keeps the
    /// prefill buffers and returns `Some(None)`.
    #[allow(clippy::too_many_arguments)]
    fn prefill_window(
        &self,
        gpu: &B,
        token_ids: &[u32],
        chunk: usize,
        state: &mut SeqState<B>,
        experts: &mut dyn ExpertSource<B>,
        cancelled: &dyn Fn() -> bool,
        more: Option<usize>,
    ) -> Result<Option<Option<B::F32>>> {
        let chunk = chunk.max(1);
        if cancelled() {
            return Ok(None);
        }
        // One chunk runs as a plain step: with little compute per layer to hide
        // copies behind, staging ahead costs more (its imprecise prediction copies
        // unused records) than it saves.
        if token_ids.len() <= chunk {
            return self
                .forward(gpu, token_ids, state, experts, &mut NoProbe, true)
                .map(|l| Some(Some(l)));
        }
        let d = &self.dims;
        let total = state.pos + token_ids.len();
        let transient = self.prefill_bytes(token_ids.len(), total, chunk);
        let reserve = more.unwrap_or(total).max(total);
        self.reserve_kv(gpu, state, reserve, transient, experts)?;
        let chunks: Vec<&[u32]> = token_ids.chunks(chunk).collect();
        let mut xs = Vec::with_capacity(chunks.len());
        for c in &chunks {
            let mut x = gpu.uninit(c.len() * d.residual())?;
            self.embed_into(gpu, c, &mut x)?;
            xs.push(x);
        }
        let per_fetch = (PREFILL_FETCH_TOKENS / chunk).max(1);
        let stage = experts.stages_ahead();
        for (layer, st) in self.layers.iter().zip(state.layers.iter_mut()) {
            if cancelled() {
                return Ok(None);
            }
            let mut pos = state.pos;
            layer.begin_prefill(gpu, st, total)?;
            let groups = chunks.len().div_ceil(per_fetch);
            for (gi, (cs, xs)) in chunks
                .chunks(per_fetch)
                .zip(xs.chunks_mut(per_fetch))
                .enumerate()
            {
                let mut pres = Vec::with_capacity(cs.len());
                for (c, x) in cs.iter().zip(xs.iter()) {
                    let step = StepInput {
                        token_ids: c,
                        start_pos: pos,
                        checkpoint: false,
                    };
                    pres.push(layer.pre_moe(gpu, d, x, c.len(), &step, st, &mut NoProbe)?);
                    pos += c.len();
                }
                // Experts staged for this layer finish reading from disk while the
                // device runs the previous layer's experts and the mixers above.
                experts.finish_stage_ahead(gpu)?;
                let lens: Vec<usize> = cs.iter().map(|c| c.len()).collect();
                let t = lens.iter().sum();
                let moe_in = layer.moe_input(gpu, d, &pres, &lens, st)?;
                let routing = layer.route(gpu, d, &moe_in, t, st)?;
                let mut union: Vec<u32> = routing.experts().collect();
                union.sort_unstable();
                union.dedup();
                let addrs = experts.fetch(gpu, layer.layer, &union)?;
                // Stage the next layer once, from the layer's last group: a group covers
                // most of a layer's experts, and staging again for every group would
                // block on the previous group's reads and copies while the device idles.
                if stage && gi + 1 == groups {
                    let predicted = layer.predict_next(gpu, d, &moe_in, t, st)?;
                    if !predicted.is_empty() {
                        experts.stage_ahead(gpu, layer.layer + 1, &predicted)?;
                    }
                }
                let mut fetched = Fetched::new(layer.layer, &union, &addrs);
                layer.finish(gpu, d, pres, &lens, &moe_in, routing, st, &mut fetched, xs)?;
                state.ws.borrow_mut().give("layer.moe_in", moe_in);
            }
            layer.end_prefill(st);
        }
        state.pos += token_ids.len();
        state.rewindable = None;
        let last = xs.last().expect("at least one chunk");
        if more.is_some() {
            return Ok(Some(None));
        }
        let t = chunks.last().map_or(0, |c| c.len());
        self.keep_hidden(gpu, state, last, t - 1, 1)?;
        let logits = self.head(gpu, &mut state.ws.borrow_mut(), last, t, &mut NoProbe, true)?;
        // Give prefill-only memory back to the expert tier for decode: the residuals,
        // the fp32 KV shadows and the grow-only buffers sized for fetch groups.
        drop(xs);
        state.ws.borrow_mut().release(PREFILL_ONLY_BUFFERS);
        self.reclaim_vram(gpu, state, experts)?;
        Ok(Some(Some(logits)))
    }

    /// Drops the last `n` tokens of the last step, which must have run with
    /// `checkpoint` and keeps at least one token. May be repeated, each time within
    /// the rows kept so far.
    pub fn rewind(&self, gpu: &B, state: &mut SeqState<B>, n: usize) -> Result<()> {
        if n == 0 {
            return Ok(());
        }
        let (start, t) = state
            .rewindable
            .context("rewind needs a checkpointed last step")?;
        ensure!(n < t, "cannot rewind {n} of a {t}-token step");
        let keep = t - n;
        for (layer, st) in self.layers.iter().zip(state.layers.iter_mut()) {
            layer.rewind(gpu, &self.dims, st, keep, start + keep)?;
        }
        state.pos = start + keep;
        state.hidden_row = keep - 1;
        // Rewinding replays from the step's start, so a later rewind within the kept
        // rows is still exact.
        state.rewindable = Some((start, keep));
        Ok(())
    }

    /// Up to `k` tokens predicted to follow `next`, the token the sequence will be
    /// fed next, from the last token's final residual (empty without an MTP head or
    /// before the first step).
    pub fn draft(
        &self,
        gpu: &B,
        state: &mut SeqState<B>,
        next: u32,
        k: usize,
        experts: &mut dyn ExpertSource<B>,
    ) -> Result<Vec<u32>> {
        let (Some(mtp), Some(hidden), Some(mst)) =
            (&self.mtp, state.hidden.as_ref(), state.mtp.as_mut())
        else {
            return Ok(Vec::new());
        };
        let d = &self.dims;
        let (h, r, hc) = (d.hidden, d.residual(), d.hc);
        let k = k.min(MAX_DRAFT);
        mtp.layer.reset_attention(mst);
        let mut ws = state.ws.borrow_mut();
        let mut x = ws.take(gpu, "mtp.x", r)?;
        gpu.copy_range(hidden, state.hidden_row * r, &mut x, 0, r)?;
        let mut drafts = Vec::with_capacity(k);
        let mut token = next;
        for s in 0..k {
            ensure!(
                (token as usize) < self.vocab,
                "token id {token} out of vocab"
            );
            let row: Vec<f32> = self.embed[token as usize * h..(token as usize + 1) * h]
                .iter()
                .map(|&w| bf16_to_f32(w))
                .collect();
            let mut scratch = ws.take_bf16(gpu, "gemm.scratch", r)?;
            let mut e = ws.take(gpu, "mtp.e", h)?;
            gpu.upload_into(&row, &mut e)?;
            let mut e_n = ws.take(gpu, "mtp.e_n", h)?;
            gpu.rmsnorm_groups(&e, &mtp.norm_embedding, &mut e_n, 1, h, h, d.eps, 1.0)?;
            let mut fe = ws.take(gpu, "mtp.fe", h)?;
            gpu.gemm_bf16(&e_n, &mtp.fc_embedding, &mut fe, &mut scratch, 1, h, h)?;
            let mut x_n = ws.take(gpu, "mtp.x_n", r)?;
            gpu.rmsnorm_groups(&x, &mtp.norm_hidden, &mut x_n, 1, r, r, d.eps, 1.0)?;
            // fc_hidden applies to each stream: the residual is `hc` rows of `hidden`.
            let mut fx = ws.take(gpu, "mtp.fx", r)?;
            gpu.gemm_bf16(&x_n, &mtp.fc_hidden, &mut fx, &mut scratch, hc, h, h)?;
            let mut fused = ws.take(gpu, "mtp.fused", r)?;
            gpu.hc_combine(&fx, &fe, &mtp.ones, &mut fused, 1, hc, h)?;
            for (name, b) in [
                ("mtp.e", e),
                ("mtp.e_n", e_n),
                ("mtp.fe", fe),
                ("mtp.x_n", x_n),
                ("mtp.fx", fx),
            ] {
                ws.give(name, b);
            }
            ws.give_bf16("gemm.scratch", scratch);
            drop(ws);
            let step = StepInput {
                token_ids: std::slice::from_ref(&token),
                start_pos: s,
                checkpoint: false,
            };
            let out = mtp
                .layer
                .forward(gpu, d, &fused, 1, &step, mst, experts, &mut NoProbe)?;
            ws = state.ws.borrow_mut();
            ws.give("mtp.fused", fused);
            let mut scratch = ws.take_bf16(gpu, "gemm.scratch", r)?;
            let (mixed, _) = mtp.mixer.mix(
                gpu,
                d,
                &mut ws,
                &out,
                1,
                &mut scratch,
                &mut NoProbe,
                "mtp_hc",
            )?;
            let mut logits = ws.take(gpu, "mtp.logits", self.vocab)?;
            gpu.gemm_bf16(
                &mixed,
                &self.lm_head,
                &mut logits,
                &mut scratch,
                1,
                self.vocab,
                h,
            )?;
            ws.give("hc.mixed", mixed);
            ws.give_bf16("gemm.scratch", scratch);
            let host = gpu.download_f32(&logits)?;
            ws.give("mtp.logits", logits);
            token = oominf_core::argmax(&host);
            drafts.push(token);
            // The next draft starts from this one's residual; the previous buffer goes
            // back for the layer's next output.
            let old = std::mem::replace(&mut x, out);
            ws.give("layer.out", old);
        }
        ws.give("mtp.x", x);
        Ok(drafts)
    }
}
