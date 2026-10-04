//! The whole text model: embedding, decoder layers, final hyper-connection mixer
//! and `lm_head`.

use anyhow::{Context, Result, ensure};
use oominf_cuda::{Bf16Buf, Buf, Gpu};
use oominf_format::Model;

use crate::hc::HyperConn;
use crate::layer::{DecoderLayer, LayerState, StepInput};
use crate::moe::ExpertSource;
use crate::util::{bf16_tensor, tap};
use crate::{Dims, Probe};

pub struct QwenModel {
    pub dims: Dims,
    vocab: usize,
    /// `embed_tokens` kept on the host as bf16 words; a step gathers its rows.
    embed: Vec<u16>,
    layers: Vec<DecoderLayer>,
    final_mixer: HyperConn,
    lm_head: Bf16Buf,
}

/// Everything carried across steps of one sequence.
pub struct SeqState {
    pub layers: Vec<LayerState>,
    /// Tokens processed so far (the next step's start position).
    pub pos: usize,
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
        Ok(SeqState {
            layers: self
                .layers
                .iter()
                .map(|l| l.new_state(gpu, &self.dims, max_tokens))
                .collect::<Result<_>>()?,
            pos: 0,
        })
    }

    /// Runs `token_ids` through the model and returns logits `[t, vocab]` (or only
    /// the last row with `last_only`). Stages are tapped as `{stage}.{layer}` plus
    /// `residual_in`, `mixer_out` and `logits`.
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
        let (t, h) = (token_ids.len(), d.hidden);
        ensure!(t > 0, "empty step");
        // Embedding, repeated over the hc streams.
        let mut host = Vec::with_capacity(t * d.residual());
        for &id in token_ids {
            ensure!((id as usize) < self.vocab, "token id {id} out of vocab");
            let row = &self.embed[id as usize * h..(id as usize + 1) * h];
            for _ in 0..d.hc {
                host.extend(row.iter().map(|&w| bf16_to_f32(w)));
            }
        }
        let mut x = gpu.upload_f32(&host)?;
        tap(gpu, probe, "residual_in", &mut x)?;

        let step = StepInput {
            token_ids,
            start_pos: state.pos,
        };
        for (layer, st) in self.layers.iter().zip(state.layers.iter_mut()) {
            let mut lp = LayerProbe {
                inner: probe,
                layer: layer.layer,
            };
            x = layer.forward(gpu, d, &x, t, &step, st, experts, &mut lp)?;
        }
        state.pos += t;

        let mut scratch = gpu.upload_u16(&vec![0u16; t * d.residual()])?;
        let (mut mixed, _) =
            self.final_mixer
                .mix(gpu, d, &x, t, &mut scratch, probe, "final_hc")?;
        tap(gpu, probe, "mixer_out", &mut mixed)?;
        let rows = if last_only { 1 } else { t };
        let input = if last_only {
            let mut last = gpu.zeros(h)?;
            gpu.copy_rows(&mixed, t - 1, 1, h, &mut last)?;
            last
        } else {
            mixed
        };
        let mut logits = gpu.zeros(rows * self.vocab)?;
        gpu.gemm_bf16(
            &input,
            &self.lm_head,
            &mut logits,
            &mut scratch,
            rows,
            self.vocab,
            h,
        )?;
        if !last_only {
            tap(gpu, probe, "logits", &mut logits)?;
        }
        Ok(logits)
    }
}
