//! The whole text model: embedding, decoder layers, final hyper-connection mixer
//! and `lm_head`.

use std::cell::RefCell;
use std::rc::Rc;

use anyhow::{Context, Result, ensure};
use oominf_cuda::{Bf16Buf, Buf, Gpu, Workspace};
use oominf_format::Model;

use crate::hc::HyperConn;
use crate::layer::{DecoderLayer, LayerState, StepInput};
use crate::moe::ExpertSource;
use crate::util::{bf16_tensor, tap};
use crate::{Dims, NoProbe, Probe};

/// Prompt tokens per prefill chunk by default: bounds prefill workspace VRAM.
pub const PREFILL_CHUNK: usize = 512;

pub struct QwenModel {
    pub dims: Dims,
    vocab: usize,
    /// `embed_tokens` kept on the host as bf16 words; a step gathers its rows.
    embed: Vec<u16>,
    layers: Vec<DecoderLayer>,
    final_mixer: HyperConn,
    lm_head: Bf16Buf,
}

/// Device memory a step's workspace may need on top of what it already holds (a
/// prefill chunk's activations and MoE scratch), kept free when caches grow.
/// Expert tiers should leave at least this much when they size themselves.
pub const WORKSPACE_HEADROOM: usize = 3 << 29;

/// Free device memory left beyond the headroom when expert tiers take memory back,
/// so a sequence's first KV growths do not immediately take it away again.
const RECLAIM_SLACK: usize = 512 << 20;

/// Everything carried across steps of one sequence.
pub struct SeqState {
    pub layers: Vec<LayerState>,
    /// Tokens processed so far (the next step's start position).
    pub pos: usize,
    /// Scratch buffers shared by every layer.
    pub ws: Rc<RefCell<Workspace>>,
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

impl QwenModel {
    /// Loads every layer's dense weights onto the GPU (experts stay on disk and come
    /// through an [`ExpertSource`]). `layers` limits how many decoder layers load.
    pub fn load(gpu: &Gpu, model: &Model, dims: Dims, layers: Option<usize>) -> Result<Self> {
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
        Ok(QwenModel {
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

    pub fn new_state(&self, gpu: &Gpu, max_tokens: usize) -> Result<SeqState> {
        let ws = Rc::new(RefCell::new(Workspace::new()));
        Ok(SeqState {
            layers: self
                .layers
                .iter()
                .map(|l| l.new_state_shared(gpu, &self.dims, max_tokens, ws.clone()))
                .collect::<Result<_>>()?,
            pos: 0,
            ws,
        })
    }

    /// Embeds `token_ids` (repeated over the hc streams) into `x`.
    fn embed_into(&self, gpu: &Gpu, token_ids: &[u32], x: &mut Buf) -> Result<()> {
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
        gpu: &Gpu,
        ws: &mut Workspace,
        x: &Buf,
        t: usize,
        probe: &mut dyn Probe,
        last_only: bool,
    ) -> Result<Buf> {
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

    /// Makes the KV caches hold `tokens`. When the growth plus `transient` bytes
    /// (other allocations the step is about to make) plus workspace headroom would
    /// not fit in free device memory, asks `experts` to give memory back first.
    pub fn reserve_kv(
        &self,
        gpu: &Gpu,
        state: &mut SeqState,
        tokens: usize,
        transient: usize,
        experts: &mut dyn ExpertSource,
    ) -> Result<()> {
        let need: usize = self
            .layers
            .iter()
            .zip(&state.layers)
            .map(|(l, s)| l.kv_growth_bytes(s, tokens))
            .sum();
        if need == 0 && transient == 0 {
            return Ok(());
        }
        gpu.sync()?;
        let (free, _) = gpu.ctx.mem_get_info()?;
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

    /// Offers `experts` the device memory a fresh `state` does not need (free memory
    /// beyond the workspace headroom and some slack), e.g. after a long sequence
    /// was dropped.
    pub fn reclaim_vram(
        &self,
        gpu: &Gpu,
        state: &SeqState,
        experts: &mut dyn ExpertSource,
    ) -> Result<()> {
        gpu.sync()?;
        let (free, _) = gpu.ctx.mem_get_info()?;
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
        gpu: &Gpu,
        token_ids: &[u32],
        state: &mut SeqState,
        experts: &mut dyn ExpertSource,
        probe: &mut dyn Probe,
        last_only: bool,
    ) -> Result<Buf> {
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
        };
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

        let mut ws = state.ws.borrow_mut();
        let logits = self.head(gpu, &mut ws, &x, t, probe, last_only)?;
        ws.give(x_name, x);
        Ok(logits)
    }

    /// Prefills a prompt layer by layer: every `chunk`-token slice of `token_ids`
    /// runs through layer 0, then through layer 1, and so on, so each layer's
    /// routed experts load about once per prompt instead of once per chunk.
    /// Within a layer the chunks run in order with that layer's state carried, so
    /// the result equals running the chunks one after another through the whole
    /// model. Returns the last row of logits, or `None` if `cancelled()` turned true
    /// (checked between layers); the state is then partially advanced and must be
    /// discarded.
    #[allow(clippy::too_many_arguments)]
    pub fn prefill(
        &self,
        gpu: &Gpu,
        token_ids: &[u32],
        chunk: usize,
        state: &mut SeqState,
        experts: &mut dyn ExpertSource,
        cancelled: &dyn Fn() -> bool,
    ) -> Result<Option<Buf>> {
        let chunk = chunk.max(1);
        if cancelled() {
            return Ok(None);
        }
        if token_ids.len() <= chunk {
            return self
                .forward(gpu, token_ids, state, experts, &mut NoProbe, true)
                .map(Some);
        }
        let d = &self.dims;
        let residuals = token_ids.len() * d.residual() * std::mem::size_of::<f32>();
        self.reserve_kv(gpu, state, state.pos + token_ids.len(), residuals, experts)?;
        let chunks: Vec<&[u32]> = token_ids.chunks(chunk).collect();
        // Residuals of every chunk live across layers (about 40 KB per token).
        let mut xs = Vec::with_capacity(chunks.len());
        for c in &chunks {
            let mut x = gpu.uninit(c.len() * d.residual())?;
            self.embed_into(gpu, c, &mut x)?;
            xs.push(x);
        }
        for (layer, st) in self.layers.iter().zip(state.layers.iter_mut()) {
            if cancelled() {
                return Ok(None);
            }
            let mut pos = state.pos;
            for (c, x) in chunks.iter().zip(xs.iter_mut()) {
                let step = StepInput {
                    token_ids: c,
                    start_pos: pos,
                };
                let next = layer.forward(gpu, d, x, c.len(), &step, st, experts, &mut NoProbe)?;
                // The chunk keeps the new residual; its old buffer goes back to the
                // workspace as the next chunk's "layer.out".
                let old = std::mem::replace(x, next);
                state.ws.borrow_mut().give("layer.out", old);
                pos += c.len();
            }
        }
        state.pos += token_ids.len();
        let last = xs.last().expect("at least one chunk");
        let t = chunks.last().map_or(0, |c| c.len());
        let mut ws = state.ws.borrow_mut();
        self.head(gpu, &mut ws, last, t, &mut NoProbe, true)
            .map(Some)
    }
}
