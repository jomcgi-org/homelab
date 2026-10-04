//! A decoder layer: optional PLE, hyper-connections around the token mixer (GDN or
//! full attention), then hyper-connections around the MoE.

use std::cell::RefCell;
use std::rc::Rc;

use anyhow::{Context, Result};
use oominf_core::{Backend, ExpertSource, NoProbe, Probe, Workspace, tap};
use oominf_format::Model;

use crate::attention::{Attention, AttnState};
use crate::gdn::{Gdn, GdnState};
use crate::hc::HyperConn;
use crate::moe::{Moe, Routing};
use crate::ple::{Ple, PleState};
use crate::{Dims, LayerKind};

pub enum Mixer<B: Backend> {
    Gdn(Gdn<B>),
    Attention(Attention<B>),
}

pub struct DecoderLayer<B: Backend> {
    pub layer: u32,
    attn_hc: HyperConn<B>,
    mixer: Mixer<B>,
    ple: Option<Ple<B>>,
    mlp_hc: HyperConn<B>,
    moe: Moe<B>,
}

/// Per-layer state carried across steps.
pub struct LayerState<B: Backend> {
    pub gdn: Option<GdnState<B>>,
    pub attn: Option<AttnState<B>>,
    pub ple: Option<PleState<B>>,
    /// Scratch buffers, shared by every layer of a sequence (layers run one at a time).
    pub ws: Rc<RefCell<Workspace<B>>>,
}

/// A layer's activations at its MoE ([`DecoderLayer::pre_moe`]): the residual
/// after the token mixer, the MoE input and the hyper-connection weights that
/// combine the MoE output back in.
pub struct PreMoe<B: Backend> {
    res1: B::F32,
    mixed: B::F32,
    inject: B::F32,
}

/// Bf16 scratch elements a layer step of `t` tokens needs.
fn scratch_len(d: &Dims, t: usize) -> usize {
    t * d.residual().max(d.conv_dim()).max(d.scratch_cols)
}

/// What a step needs to know beyond activations.
pub struct StepInput<'a> {
    /// Token ids of this step (PLE hashes them).
    pub token_ids: &'a [u32],
    /// Absolute position of the step's first token.
    pub start_pos: usize,
    /// Keep what is needed to rewind this step to any of its rows
    /// ([`DecoderLayer::rewind`]).
    pub checkpoint: bool,
}

impl<B: Backend> DecoderLayer<B> {
    /// Lets decode-sized steps compute host-resident experts on `pool`.
    pub fn set_host_experts(&mut self, pool: std::sync::Arc<oominf_cpu::HostExperts>) {
        self.moe.set_host_experts(pool);
    }

    pub fn load(gpu: &B, model: &Model, d: &Dims, layer: u32) -> Result<Self> {
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

    /// The multi-token-prediction layer: a full-attention layer named `mtp.layers.0`
    /// whose routed experts are expert group `group`.
    pub fn load_mtp(gpu: &B, model: &Model, d: &Dims, group: u32) -> Result<Self> {
        let p = "mtp.layers.0";
        Ok(DecoderLayer {
            layer: group,
            attn_hc: HyperConn::load(gpu, model, d, &format!("{p}.attn_hyper_connection"), true)?,
            mixer: Mixer::Attention(Attention::load_prefixed(
                gpu,
                model,
                d,
                &format!("{p}.self_attn."),
            )?),
            ple: None,
            mlp_hc: HyperConn::load(gpu, model, d, &format!("{p}.mlp_hyper_connection"), true)?,
            moe: Moe::load_group(gpu, model, d, group, &format!("{p}.mlp."), None)?,
        })
    }

    pub fn new_state(&self, gpu: &B, d: &Dims, max_tokens: usize) -> Result<LayerState<B>> {
        self.new_state_shared(gpu, d, max_tokens, Rc::new(RefCell::new(Workspace::new())))
    }

    /// Like [`Self::new_state`], with a workspace shared across layers.
    pub fn new_state_shared(
        &self,
        gpu: &B,
        d: &Dims,
        max_tokens: usize,
        ws: Rc<RefCell<Workspace<B>>>,
    ) -> Result<LayerState<B>> {
        Ok(LayerState {
            ws,
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

    /// Returns this layer's state to just after the first `keep` rows of the last
    /// step, which must have been run with [`StepInput::checkpoint`]; `len` is the
    /// sequence length after those rows.
    pub fn rewind(
        &self,
        gpu: &B,
        d: &Dims,
        state: &mut LayerState<B>,
        keep: usize,
        len: usize,
    ) -> Result<()> {
        let mut ws = state.ws.borrow_mut();
        match (&self.mixer, state.gdn.as_mut(), state.attn.as_mut()) {
            (Mixer::Gdn(g), Some(st), _) => g.rewind(gpu, d, &mut ws, st, keep)?,
            (Mixer::Attention(a), _, Some(st)) => a.rewind(st, len),
            _ => anyhow::bail!("layer {} has no state for its mixer", self.layer),
        }
        if let (Some(p), Some(st)) = (&self.ple, state.ple.as_mut()) {
            p.rewind(gpu, d, &mut ws, st, keep)?;
        }
        Ok(())
    }

    /// Forgets every token this layer's attention cached (for a draft chain that
    /// restarts each step).
    pub fn reset_attention(&self, state: &mut LayerState<B>) {
        if let (Mixer::Attention(a), Some(st)) = (&self.mixer, state.attn.as_mut()) {
            a.reset(st);
        }
    }

    /// Bytes [`Self::grow_kv`] would allocate for this layer to hold `tokens`.
    pub fn kv_growth_bytes(&self, state: &LayerState<B>, tokens: usize) -> usize {
        match (&self.mixer, &state.attn) {
            (Mixer::Attention(a), Some(st)) => a.growth_bytes(st, tokens),
            _ => 0,
        }
    }

    /// Grows this layer's KV cache (if it has one) to hold `tokens`.
    pub fn grow_kv(&self, gpu: &B, state: &mut LayerState<B>, tokens: usize) -> Result<()> {
        match (&self.mixer, state.attn.as_mut()) {
            (Mixer::Attention(a), Some(st)) => a.grow(gpu, st, tokens),
            _ => Ok(()),
        }
    }

    /// Runs the layer over `t` tokens of `residual` (`[t, hc * hidden]`).
    #[allow(clippy::too_many_arguments)]
    pub fn forward(
        &self,
        gpu: &B,
        d: &Dims,
        residual: &B::F32,
        t: usize,
        step: &StepInput,
        state: &mut LayerState<B>,
        experts: &mut dyn ExpertSource<B>,
        probe: &mut dyn Probe,
    ) -> Result<B::F32> {
        let pre = self.pre_moe(gpu, d, residual, t, step, state, probe)?;
        let ws_rc = state.ws.clone();
        let mut ws = ws_rc.borrow_mut();
        let mut scratch = ws.take_bf16(gpu, "gemm.scratch", scratch_len(d, t))?;
        let moe_out =
            self.moe
                .forward(gpu, d, &mut ws, &pre.mixed, t, experts, &mut scratch, probe)?;
        ws.give_bf16("gemm.scratch", scratch);
        drop(ws);
        self.post_moe(gpu, d, pre, &moe_out, t, state, probe)
    }

    /// Routes `pre` (from [`Self::pre_moe`]) with this layer's router.
    pub fn route(
        &self,
        gpu: &B,
        d: &Dims,
        pre: &PreMoe<B>,
        t: usize,
        state: &LayerState<B>,
    ) -> Result<Routing<B>> {
        let mut ws = state.ws.borrow_mut();
        let mut scratch = ws.take_bf16(gpu, "gemm.scratch", scratch_len(d, t))?;
        let routing = self.moe.route(gpu, d, &pre.mixed, t, &mut scratch);
        ws.give_bf16("gemm.scratch", scratch);
        routing
    }

    /// The next layer's experts predicted for `pre` (see [`Moe::predict_next`]).
    pub fn predict_next(
        &self,
        gpu: &B,
        d: &Dims,
        pre: &PreMoe<B>,
        t: usize,
        state: &LayerState<B>,
    ) -> Result<Vec<u32>> {
        let mut ws = state.ws.borrow_mut();
        let mut scratch = ws.take_bf16(gpu, "gemm.scratch", scratch_len(d, t))?;
        let next = self.moe.predict_next(gpu, d, &pre.mixed, t, &mut scratch);
        ws.give_bf16("gemm.scratch", scratch);
        next
    }

    /// Finishes the layer for `pre` routed by `routing` (from [`Self::route`]),
    /// with records from `experts`.
    #[allow(clippy::too_many_arguments)]
    pub fn finish(
        &self,
        gpu: &B,
        d: &Dims,
        pre: PreMoe<B>,
        routing: Routing<B>,
        t: usize,
        state: &mut LayerState<B>,
        experts: &mut dyn ExpertSource<B>,
    ) -> Result<B::F32> {
        let ws_rc = state.ws.clone();
        let mut ws = ws_rc.borrow_mut();
        let mut scratch = ws.take_bf16(gpu, "gemm.scratch", scratch_len(d, t))?;
        let moe_out = self.moe.apply(
            gpu,
            d,
            &mut ws,
            &pre.mixed,
            t,
            routing,
            experts,
            &mut scratch,
        )?;
        ws.give_bf16("gemm.scratch", scratch);
        drop(ws);
        self.post_moe(gpu, d, pre, &moe_out, t, state, &mut NoProbe)
    }

    /// The layer up to its MoE: PLE, the token mixer and the hyper-connections into
    /// the MoE.
    #[allow(clippy::too_many_arguments)]
    pub fn pre_moe(
        &self,
        gpu: &B,
        d: &Dims,
        residual: &B::F32,
        t: usize,
        step: &StepInput,
        state: &mut LayerState<B>,
        probe: &mut dyn Probe,
    ) -> Result<PreMoe<B>> {
        let ws_rc = state.ws.clone();
        let mut ws_guard = ws_rc.borrow_mut();
        let ws: &mut Workspace<B> = &mut ws_guard;
        let r = d.residual();
        let mut scratch = ws.take_bf16(gpu, "gemm.scratch", scratch_len(d, t))?;

        let mut with_ple = None;
        if let Some(ple) = &self.ple {
            let st = state.ple.as_mut().context("PLE layer without PLE state")?;
            let mut add = ple.forward(gpu, d, ws, residual, t, step, st, &mut scratch, probe)?;
            tap(gpu, probe, "ple_out", &mut add)?;
            let mut sum = ws.take(gpu, "layer.ple_sum", t * r)?;
            gpu.add(residual, &add, &mut sum, t * r)?;
            ws.give("ple.out", add);
            with_ple = Some(sum);
        }
        let residual = with_ple.as_ref().unwrap_or(residual);

        let (mixed, inject) =
            self.attn_hc
                .mix(gpu, d, ws, residual, t, &mut scratch, probe, "attn_hc")?;
        let inject = inject.context("attention hyper-connection without combine")?;
        let (mut mixer_out, out_name) = match &self.mixer {
            Mixer::Gdn(g) => {
                let st = state.gdn.as_mut().context("GDN layer without GDN state")?;
                (
                    g.forward(
                        gpu,
                        d,
                        ws,
                        &mixed,
                        t,
                        st,
                        step.checkpoint,
                        &mut scratch,
                        probe,
                    )?,
                    "gdn.out",
                )
            }
            Mixer::Attention(a) => {
                let st = state
                    .attn
                    .as_mut()
                    .context("attention layer without KV state")?;
                (
                    a.forward(gpu, d, ws, &mixed, t, step, st, &mut scratch, probe)?,
                    "attn.out",
                )
            }
        };
        ws.give("hc.mixed", mixed);
        tap(gpu, probe, "mixer_out", &mut mixer_out)?;
        let mut res1 = ws.take(gpu, "layer.res1", t * r)?;
        HyperConn::combine_into(gpu, d, residual, &mixer_out, &inject, t, &mut res1)?;
        ws.give(out_name, mixer_out);
        ws.give("hc.inject", inject);
        if let Some(sum) = with_ple {
            ws.give("layer.ple_sum", sum);
        }
        tap(gpu, probe, "attn_combine_out", &mut res1)?;

        let (mixed, inject) =
            self.mlp_hc
                .mix(gpu, d, ws, &res1, t, &mut scratch, probe, "mlp_hc")?;
        let inject = inject.context("MLP hyper-connection without combine")?;
        ws.give_bf16("gemm.scratch", scratch);
        Ok(PreMoe {
            res1,
            mixed,
            inject,
        })
    }

    /// The layer output from `pre` and the MoE output.
    #[allow(clippy::too_many_arguments)]
    fn post_moe(
        &self,
        gpu: &B,
        d: &Dims,
        pre: PreMoe<B>,
        moe_out: &B::F32,
        t: usize,
        state: &mut LayerState<B>,
        probe: &mut dyn Probe,
    ) -> Result<B::F32> {
        let mut ws = state.ws.borrow_mut();
        let PreMoe {
            res1,
            mixed,
            inject,
        } = pre;
        ws.give("hc.mixed", mixed);
        // The layer output is handed to the caller; the model gives the previous
        // residual back under the same name so two buffers alternate.
        let mut out = ws.take(gpu, "layer.out", t * d.residual())?;
        HyperConn::combine_into(gpu, d, &res1, moe_out, &inject, t, &mut out)?;
        ws.give("hc.inject", inject);
        ws.give("layer.res1", res1);
        drop(ws);
        tap(gpu, probe, "layer_out", &mut out)?;
        if let Some(g) = state.gdn.as_mut() {
            tap(gpu, probe, "state.conv", &mut g.conv)?;
            tap(gpu, probe, "state.recurrent", &mut g.recurrent)?;
        }
        Ok(out)
    }
}
