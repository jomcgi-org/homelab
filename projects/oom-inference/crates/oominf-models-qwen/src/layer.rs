//! A decoder layer: optional PLE, hyper-connections around the token mixer (GDN or
//! full attention), then hyper-connections around the MoE.

use anyhow::{Context, Result};
use oominf_cuda::{Bf16Buf, Buf, Gpu};
use oominf_format::Model;

use crate::attention::{Attention, AttnState};
use crate::gdn::{Gdn, GdnState};
use crate::hc::HyperConn;
use crate::moe::{ExpertSource, Moe};
use crate::ple::{Ple, PleState};
use crate::util::tap;
use crate::{Dims, LayerKind, Probe};

#[allow(clippy::large_enum_variant)] // shrinks once Attention has weights
pub enum Mixer {
    Gdn(Gdn),
    Attention(Attention),
}

pub struct DecoderLayer {
    pub layer: u32,
    attn_hc: HyperConn,
    mixer: Mixer,
    ple: Option<Ple>,
    mlp_hc: HyperConn,
    moe: Moe,
}

/// Per-layer state carried across steps.
pub struct LayerState {
    pub gdn: Option<GdnState>,
    pub attn: Option<AttnState>,
    pub ple: Option<PleState>,
}

/// What a step needs to know beyond activations.
pub struct StepInput<'a> {
    /// Token ids of this step (PLE hashes them).
    pub token_ids: &'a [u32],
    /// Absolute position of the step's first token.
    pub start_pos: usize,
}

impl DecoderLayer {
    pub fn load(gpu: &Gpu, model: &Model, d: &Dims, layer: u32) -> Result<Self> {
        let p = format!("model.language_model.layers.{layer}");
        let kind = d
            .layer_kinds
            .get(layer as usize)
            .with_context(|| format!("layer {layer} beyond config layer_types"))?;
        let mixer = match kind {
            LayerKind::Linear => Mixer::Gdn(Gdn::load(gpu, model, d, layer)?),
            LayerKind::Full => Mixer::Attention(Attention::load(gpu, model, d, layer)?),
        };
        let ple = if d.ple_layers.contains(&layer) {
            Some(Ple::load(gpu, model, d, layer)?)
        } else {
            None
        };
        Ok(DecoderLayer {
            layer,
            attn_hc: HyperConn::load(gpu, model, d, &format!("{p}.attn_hyper_connection"), true)?,
            mixer,
            ple,
            mlp_hc: HyperConn::load(gpu, model, d, &format!("{p}.mlp_hyper_connection"), true)?,
            moe: Moe::load(gpu, model, d, layer)?,
        })
    }

    pub fn new_state(&self, gpu: &Gpu, d: &Dims, max_tokens: usize) -> Result<LayerState> {
        Ok(LayerState {
            gdn: match self.mixer {
                Mixer::Gdn(_) => Some(GdnState::new(gpu, d)?),
                Mixer::Attention(_) => None,
            },
            attn: match &self.mixer {
                Mixer::Attention(a) => Some(a.new_state(gpu, d, max_tokens)?),
                Mixer::Gdn(_) => None,
            },
            ple: match &self.ple {
                Some(p) => Some(p.new_state(gpu, d)?),
                None => None,
            },
        })
    }

    /// Runs the layer over `t` tokens of `residual` (`[t, hc * hidden]`).
    #[allow(clippy::too_many_arguments)]
    pub fn forward(
        &self,
        gpu: &Gpu,
        d: &Dims,
        residual: &Buf,
        t: usize,
        step: &StepInput,
        state: &mut LayerState,
        experts: &mut dyn ExpertSource,
        probe: &mut dyn Probe,
    ) -> Result<Buf> {
        let mut scratch: Bf16Buf =
            gpu.upload_u16(&vec![
                0u16;
                t * d.residual().max(d.conv_dim()).max(d.scratch_cols)
            ])?;

        let with_ple;
        let residual = match &self.ple {
            Some(ple) => {
                let st = state.ple.as_mut().context("PLE layer without PLE state")?;
                let mut add = ple.forward(gpu, d, residual, t, step, st, &mut scratch, probe)?;
                tap(gpu, probe, "ple_out", &mut add)?;
                let mut sum = gpu.zeros(t * d.residual())?;
                gpu.add(residual, &add, &mut sum, t * d.residual())?;
                with_ple = sum;
                &with_ple
            }
            None => residual,
        };

        let (mixed, inject) =
            self.attn_hc
                .mix(gpu, d, residual, t, &mut scratch, probe, "attn_hc")?;
        let mut mixer_out = match &self.mixer {
            Mixer::Gdn(g) => {
                let st = state.gdn.as_mut().context("GDN layer without GDN state")?;
                g.forward(gpu, d, &mixed, t, st, &mut scratch, probe)?
            }
            Mixer::Attention(a) => {
                let st = state
                    .attn
                    .as_mut()
                    .context("attention layer without KV state")?;
                a.forward(gpu, d, &mixed, t, step, st, &mut scratch, probe)?
            }
        };
        tap(gpu, probe, "mixer_out", &mut mixer_out)?;
        let mut res1 =
            HyperConn::combine(gpu, d, residual, &mixer_out, inject.as_ref().unwrap(), t)?;
        tap(gpu, probe, "attn_combine_out", &mut res1)?;

        let (mixed, inject) = self
            .mlp_hc
            .mix(gpu, d, &res1, t, &mut scratch, probe, "mlp_hc")?;
        let moe_out = self
            .moe
            .forward(gpu, d, &mixed, t, experts, &mut scratch, probe)?;
        let mut out = HyperConn::combine(gpu, d, &res1, &moe_out, inject.as_ref().unwrap(), t)?;
        tap(gpu, probe, "layer_out", &mut out)?;
        if let Some(g) = state.gdn.as_mut() {
            tap(gpu, probe, "state.conv", &mut g.conv)?;
            tap(gpu, probe, "state.recurrent", &mut g.recurrent)?;
        }
        Ok(out)
    }
}
