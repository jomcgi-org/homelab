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
use crate::moe::{HostIds, Moe, PendingRoute, Routing};
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

    /// Forgets every token this layer's attention cached; the next one cached is at
    /// position `base`.
    pub fn reset_attention_at(&self, state: &mut LayerState<B>, base: usize) {
        if let (Mixer::Attention(a), Some(st)) = (&self.mixer, state.attn.as_mut()) {
            a.reset(st);
            st.base = base;
        }
    }

    /// Returns this layer to its state after the first `pos` tokens: the attention
    /// cache is truncated, recurrent state comes from `snap`.
    pub fn restore_checkpoint(
        &self,
        gpu: &B,
        state: &mut LayerState<B>,
        snap: &crate::checkpoint::LayerSnap,
        pos: usize,
    ) -> Result<()> {
        if let (Mixer::Attention(a), Some(st)) = (&self.mixer, state.attn.as_mut()) {
            anyhow::ensure!(
                st.base == 0 && st.len >= pos,
                "attention cache holds {} tokens, rewinding to {pos}",
                st.len
            );
            a.rewind(st, pos);
        }
        crate::checkpoint::restore(gpu, state, snap)
    }

    /// The position of this layer's first cached token and how many it holds
    /// (`None` without attention).
    pub fn attention_extent(&self, state: &LayerState<B>) -> Option<(usize, usize)> {
        state.attn.as_ref().map(|st| (st.base, st.len))
    }

    /// Bytes of this layer's sequence-length-dependent step buffers for a step of
    /// `t` tokens over `kv_len` cached ones (0 without attention).
    pub fn step_bytes(&self, t: usize, kv_len: usize) -> usize {
        match &self.mixer {
            Mixer::Attention(a) => a.step_bytes(t, kv_len),
            Mixer::Gdn(_) => 0,
        }
    }

    /// What this layer's attention cache holds (see [`Attention::cache_extent`]);
    /// zeros without attention.
    pub fn cache_extent(&self, state: &AttnState<B>) -> (usize, usize, usize, usize) {
        match &self.mixer {
            Mixer::Attention(a) => a.cache_extent(state),
            Mixer::Gdn(_) => (0, 0, 0, 0),
        }
    }

    /// Bytes of the fp32 KV shadow a layer-major prefill of up to `tokens` tokens
    /// holds while this layer runs (0 without attention or with an fp32 cache).
    pub fn shadow_bytes(&self, tokens: usize) -> usize {
        match &self.mixer {
            Mixer::Attention(a) => a.shadow_bytes(tokens),
            Mixer::Gdn(_) => 0,
        }
    }

    /// Starts this layer's layer-major prefill of up to `tokens` tokens (see
    /// [`Attention::begin_shadow`]).
    pub fn begin_prefill(&self, gpu: &B, state: &mut LayerState<B>, tokens: usize) -> Result<()> {
        if let (Mixer::Attention(a), Some(st)) = (&self.mixer, state.attn.as_mut()) {
            a.begin_shadow(gpu, &mut state.ws.borrow_mut(), st, tokens)?;
        }
        Ok(())
    }

    /// Ends this layer's layer-major prefill.
    pub fn end_prefill(&self, state: &mut LayerState<B>) {
        if let (Mixer::Attention(a), Some(st)) = (&self.mixer, state.attn.as_mut()) {
            a.end_shadow(&mut state.ws.borrow_mut(), st);
        }
    }

    /// Device bytes this layer's state for one sequence of up to `max_tokens` holds
    /// at `tokens` tokens: the KV cache, or the recurrent and conv state with the
    /// copies a rewindable step keeps (decode-sized steps), and PLE conv state.
    pub fn state_bytes(&self, d: &Dims, tokens: usize, max_tokens: usize) -> usize {
        let f = std::mem::size_of::<f32>();
        let mixer = match &self.mixer {
            Mixer::Attention(a) => a.state_bytes(tokens, max_tokens),
            Mixer::Gdn(_) => {
                2 * (d.conv_dim() * d.conv_kernel + d.v_heads * d.head_k * d.head_v) * f
            }
        };
        // PLE conv state ((kernel - 1) * dilation residual rows, 9 for this
        // family, bounded here by 16) and its rewind copy.
        let ple = if self.ple.is_some() {
            2 * d.residual() * 16 * f
        } else {
            0
        };
        mixer + ple
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

    /// Runs the layer over several sequences' rows in one step: each sequence's
    /// token mixer (PLE, GDN or attention, and their hyper-connections) runs on its
    /// own state, then their MoE inputs are concatenated and the MoE runs once over
    /// all rows (one routing, one fetch of the union of their experts, one decode
    /// expert launch), and each sequence's MoE output is combined back on its own.
    /// `xs[i]` holds `steps[i].token_ids.len()` residual rows of sequence `i`, whose
    /// layer state is `states[i]`. Returns each sequence's layer output, taken from
    /// its own workspace as `layer.out` (as [`Self::forward`]). Shared buffers come
    /// from `ws`.
    #[allow(clippy::too_many_arguments)]
    pub fn forward_many(
        &self,
        gpu: &B,
        d: &Dims,
        ws: &RefCell<Workspace<B>>,
        xs: &[&B::F32],
        steps: &[StepInput],
        states: &mut [&mut LayerState<B>],
        experts: &mut dyn ExpertSource<B>,
    ) -> Result<Vec<B::F32>> {
        let h = d.hidden;
        let lens: Vec<usize> = steps.iter().map(|s| s.token_ids.len()).collect();
        let total: usize = lens.iter().sum();
        let mut pres = Vec::with_capacity(xs.len());
        for ((x, step), st) in xs.iter().zip(steps).zip(states.iter_mut()) {
            pres.push(self.pre_moe(gpu, d, x, step.token_ids.len(), step, st, &mut NoProbe)?);
        }
        let mut wsb = ws.borrow_mut();
        let mut moe_in = wsb.take(gpu, "layer.many_in", total * h)?;
        let mut at = 0;
        for (pre, &t) in pres.iter().zip(&lens) {
            gpu.copy_range(&pre.mixed, 0, &mut moe_in, at * h, t * h)?;
            at += t;
        }
        let mut scratch = wsb.take_bf16(gpu, "gemm.scratch", scratch_len(d, total))?;
        let moe_out = self.moe.forward(
            gpu,
            d,
            &mut wsb,
            &moe_in,
            total,
            experts,
            &mut scratch,
            &mut NoProbe,
        )?;
        wsb.give_bf16("gemm.scratch", scratch);
        wsb.give("layer.many_in", moe_in);
        drop(wsb);
        let mut outs = Vec::with_capacity(xs.len());
        let mut at = 0;
        for ((pre, &t), st) in pres.into_iter().zip(&lens).zip(states.iter_mut()) {
            let mut part = st.ws.borrow_mut().take(gpu, "layer.many_part", t * h)?;
            gpu.copy_range(&moe_out, at * h, &mut part, 0, t * h)?;
            outs.push(self.post_moe(gpu, d, pre, &part, t, st, &mut NoProbe)?);
            st.ws.borrow_mut().give("layer.many_part", part);
            at += t;
        }
        Ok(outs)
    }

    /// The MoE inputs of `pres` (from [`Self::pre_moe`], `lens[i]` tokens each) in one
    /// `[sum(lens), hidden]` buffer, so their routed experts run as one step. The
    /// buffer is the workspace's grow-only `name`; give it back after
    /// [`Self::finish`].
    pub fn moe_input(
        &self,
        gpu: &B,
        d: &Dims,
        pres: &[PreMoe<B>],
        lens: &[usize],
        state: &LayerState<B>,
        name: &'static str,
    ) -> Result<B::F32> {
        let h = d.hidden;
        let n = lens.iter().sum::<usize>() * h;
        let mut x = state.ws.borrow_mut().take_at_least(gpu, name, n)?;
        let mut at = 0;
        for (pre, &t) in pres.iter().zip(lens) {
            gpu.copy_range(&pre.mixed, 0, &mut x, at * h, t * h)?;
            at += t;
        }
        Ok(x)
    }

    /// Queues routing of `t` tokens of MoE input `x` (from [`Self::moe_input`]); see
    /// [`Moe::route_start`].
    #[allow(clippy::too_many_arguments)]
    pub fn route_start(
        &self,
        gpu: &B,
        d: &Dims,
        x: &B::F32,
        t: usize,
        state: &LayerState<B>,
        with_next: bool,
        host: &mut HostIds<'_, B>,
    ) -> Result<PendingRoute<B>> {
        let mut ws = state.ws.borrow_mut();
        let mut scratch = ws.take_bf16(gpu, "gemm.scratch", scratch_len(d, t))?;
        let pending = self
            .moe
            .route_start(gpu, d, x, t, &mut scratch, with_next, host);
        ws.give_bf16("gemm.scratch", scratch);
        pending
    }

    /// Whether routing queued by [`Self::route_start`] has landed.
    pub fn route_ready(&self, gpu: &B, pending: &crate::moe::PendingRoute<B>) -> Result<bool> {
        self.moe.route_ready(gpu, pending)
    }

    /// Collects routing queued by [`Self::route_start`]; see [`Moe::route_finish`].
    pub fn route_finish(
        &self,
        gpu: &B,
        d: &Dims,
        pending: PendingRoute<B>,
        host: &HostIds<'_, B>,
    ) -> Result<(Routing<B>, Vec<u32>)> {
        self.moe.route_finish(gpu, d, pending, host)
    }

    /// Finishes the layer for `pres` (`lens[i]` tokens each) whose MoE input `x`
    /// (from [`Self::moe_input`]) is routed by `routing` (from [`Self::route_finish`]),
    /// with records from `experts`: their routed experts run as one step. Writes each
    /// chunk's layer output over its residual in `outs` (which [`Self::pre_moe`] no
    /// longer needs), so residuals living across layers are not reallocated among
    /// the step's temporaries, which would fragment the allocator.
    #[allow(clippy::too_many_arguments)]
    pub fn finish(
        &self,
        gpu: &B,
        d: &Dims,
        pres: Vec<PreMoe<B>>,
        lens: &[usize],
        x: &B::F32,
        routing: Routing<B>,
        state: &mut LayerState<B>,
        experts: &mut dyn ExpertSource<B>,
        outs: &mut [B::F32],
    ) -> Result<()> {
        let h = d.hidden;
        let total = lens.iter().sum();
        let ws_rc = state.ws.clone();
        let mut ws = ws_rc.borrow_mut();
        let mut scratch = ws.take_bf16(gpu, "gemm.scratch", scratch_len(d, total))?;
        let moe_out = self
            .moe
            .apply(gpu, d, &mut ws, x, total, routing, experts, &mut scratch)?;
        ws.give_bf16("gemm.scratch", scratch);
        let mut part =
            ws.take_at_least(gpu, "layer.moe_part", lens.iter().max().unwrap_or(&1) * h)?;
        drop(ws);
        let mut at = 0;
        for ((pre, &t), out) in pres.into_iter().zip(lens).zip(outs.iter_mut()) {
            gpu.copy_range(&moe_out, at * h, &mut part, 0, t * h)?;
            self.post_moe_into(gpu, d, pre, &part, t, state, &mut NoProbe, out)?;
            at += t;
        }
        state.ws.borrow_mut().give("layer.moe_part", part);
        Ok(())
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
        // The layer output is handed to the caller; the model gives the previous
        // residual back under the same name so two buffers alternate.
        let mut out = state
            .ws
            .borrow_mut()
            .take(gpu, "layer.out", t * d.residual())?;
        self.post_moe_into(gpu, d, pre, moe_out, t, state, probe, &mut out)?;
        Ok(out)
    }

    /// [`Self::post_moe`] into `out` (`t` residual rows).
    #[allow(clippy::too_many_arguments)]
    fn post_moe_into(
        &self,
        gpu: &B,
        d: &Dims,
        pre: PreMoe<B>,
        moe_out: &B::F32,
        t: usize,
        state: &mut LayerState<B>,
        probe: &mut dyn Probe,
        out: &mut B::F32,
    ) -> Result<()> {
        let mut ws = state.ws.borrow_mut();
        let PreMoe {
            res1,
            mixed,
            inject,
        } = pre;
        ws.give("hc.mixed", mixed);
        HyperConn::combine_into(gpu, d, &res1, moe_out, &inject, t, out)?;
        ws.give("hc.inject", inject);
        ws.give("layer.res1", res1);
        drop(ws);
        tap(gpu, probe, "layer_out", out)?;
        if let Some(g) = state.gdn.as_mut() {
            tap(gpu, probe, "state.conv", &mut g.conv)?;
            tap(gpu, probe, "state.recurrent", &mut g.recurrent)?;
        }
        Ok(())
    }
}
