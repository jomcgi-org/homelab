//! Gated DeltaNet, the token mixer of linear-attention layers.

use anyhow::{Context, Result, ensure};
use oominf_core::{Backend, DeviceBuffer, Probe, Weight, Workspace, tap, tap_cols};
use oominf_format::Model;

use crate::Dims;
use crate::util::{bf16_tensor, weight, weight_concat};

pub struct Gdn<B: Backend> {
    /// `in_proj_qkv`, `in_proj_z`, `in_proj_b`, `in_proj_a` stacked: one GEMM.
    in_proj: Weight<B>,
    conv: B::Bf16,
    a_log: B::Bf16,
    dt_bias: B::Bf16,
    norm: B::Bf16,
    out: Weight<B>,
}

/// Recurrent GDN state carried across steps.
pub struct GdnState<B: Backend> {
    /// `[conv_dim, conv_kernel]` last inputs, oldest first.
    pub conv: B::F32,
    /// `[v_heads, head_k, head_v]`.
    pub recurrent: B::F32,
    /// What the last checkpointed step needs to rewind (see [`Gdn::rewind`]).
    ckpt: Option<Checkpoint<B>>,
}

/// A step's starting state and per-row inputs: rewinding restores the state and
/// replays the conv and the recurrence over the rows kept, which reproduces exactly
/// the state after those rows.
struct Checkpoint<B: Backend> {
    conv: B::F32,
    recurrent: B::F32,
    /// The step's fused projection rows `[t, n]` (conv input at column 0).
    proj: B::F32,
    /// Conv outputs with q and k L2-normalised `[t, conv_dim]`.
    qkv: B::F32,
    g: B::F32,
    beta: B::F32,
    t: usize,
}

impl<B: Backend> GdnState<B> {
    pub fn new(gpu: &B, d: &Dims) -> Result<Self> {
        Ok(GdnState {
            conv: gpu.zeros(d.conv_dim() * d.conv_kernel)?,
            recurrent: gpu.zeros(d.v_heads * d.head_k * d.head_v)?,
            ckpt: None,
        })
    }
}

/// `buf` holding at least `n` values (reused when it already does).
fn sized<B: Backend>(gpu: &B, buf: Option<B::F32>, n: usize) -> Result<B::F32> {
    match buf {
        Some(b) if b.len() >= n => Ok(b),
        _ => gpu.uninit(n),
    }
}

impl<B: Backend> Gdn<B> {
    pub fn load(gpu: &B, model: &Model, d: &Dims, layer: u32) -> Result<Self> {
        let la = format!("model.language_model.layers.{layer}.linear_attn.");
        let (h, kd, vd, cd, hv) = (
            d.hidden as u64,
            d.key_dim() as u64,
            d.value_dim() as u64,
            d.conv_dim() as u64,
            d.v_heads as u64,
        );
        let w = |n: &str, s: &[u64]| bf16_tensor(gpu, model, &format!("{la}{n}"), s);
        Ok(Gdn {
            in_proj: weight_concat(
                gpu,
                model,
                &[
                    (format!("{la}in_proj_qkv.weight"), vec![2 * kd + vd, h]),
                    (format!("{la}in_proj_z.weight"), vec![vd, h]),
                    (format!("{la}in_proj_b.weight"), vec![hv, h]),
                    (format!("{la}in_proj_a.weight"), vec![hv, h]),
                ],
                d,
            )?,
            conv: w("conv1d.weight", &[cd, d.conv_kernel as u64])?,
            a_log: w("A_log", &[hv])?,
            dt_bias: w("dt_bias", &[hv])?,
            norm: w("norm.weight", &[d.head_v as u64])?,
            out: weight(gpu, model, &format!("{la}out_proj.weight"), &[h, vd], d)?,
        })
    }

    /// Returns the mixer output `[t, hidden]` as workspace buffer `gdn.out`; the
    /// caller gives it back. With `checkpoint`, the step can later be rewound to any
    /// of its rows ([`Gdn::rewind`]).
    #[allow(clippy::too_many_arguments)]
    pub fn forward(
        &self,
        gpu: &B,
        d: &Dims,
        ws: &mut Workspace<B>,
        x: &B::F32,
        t: usize,
        state: &mut GdnState<B>,
        checkpoint: bool,
        scratch: &mut B::Bf16,
        probe: &mut dyn Probe,
    ) -> Result<B::F32> {
        let (cd, vd, hv) = (d.conv_dim(), d.value_dim(), d.v_heads);
        let n = self.proj_width(d);
        let mut proj = self.project(gpu, d, ws, x, t, scratch)?;
        // Downstream kernels read the fused projection in place:
        // [qkv (cd) | z (vd) | b (hv) | a (hv)] per row.
        let (z_off, b_off, a_off) = (cd, cd + vd, cd + vd + hv);
        tap_cols(gpu, probe, "gdn.in_proj_qkv", &mut proj, t, n, 0, cd)?;
        tap_cols(gpu, probe, "gdn.in_proj_z", &mut proj, t, n, z_off, vd)?;
        tap_cols(gpu, probe, "gdn.in_proj_b", &mut proj, t, n, b_off, hv)?;
        tap_cols(gpu, probe, "gdn.in_proj_a", &mut proj, t, n, a_off, hv)?;
        let normed = self.core(gpu, d, ws, &proj, t, state, checkpoint, probe)?;
        ws.give("gdn.proj", proj);
        let out = self.output(gpu, d, ws, &normed, t, scratch)?;
        ws.give("gdn.normed", normed);
        Ok(out)
    }

    /// Width of [`Self::project`]'s rows.
    pub fn proj_width(&self, d: &Dims) -> usize {
        d.conv_dim() + d.value_dim() + 2 * d.v_heads
    }

    /// The fused input projection of `t` rows of `x` (workspace buffer
    /// `gdn.proj`). Rows are independent: several sequences' rows can run as one.
    pub fn project(
        &self,
        gpu: &B,
        d: &Dims,
        ws: &mut Workspace<B>,
        x: &B::F32,
        t: usize,
        scratch: &mut B::Bf16,
    ) -> Result<B::F32> {
        let n = self.proj_width(d);
        let mut proj = ws.take(gpu, "gdn.proj", t * n)?;
        gpu.gemm_w(x, &self.in_proj, &mut proj, scratch, t, n, d.hidden)?;
        Ok(proj)
    }

    /// The output projection of `t` normed rows (workspace buffer `gdn.out`).
    pub fn output(
        &self,
        gpu: &B,
        d: &Dims,
        ws: &mut Workspace<B>,
        normed: &B::F32,
        t: usize,
        scratch: &mut B::Bf16,
    ) -> Result<B::F32> {
        let (vd, h) = (d.value_dim(), d.hidden);
        let mut out = ws.take(gpu, "gdn.out", t * h)?;
        gpu.gemm_w(normed, &self.out, &mut out, scratch, t, h, vd)?;
        Ok(out)
    }

    /// One sequence's recurrence over its `t` projected rows (from
    /// [`Self::project`]): causal conv, gates, the gated delta rule on `state` and
    /// the gated norm. Returns the normed rows `[t, value_dim]` as workspace buffer
    /// `gdn.normed`. With `checkpoint`, the step can later be rewound.
    #[allow(clippy::too_many_arguments)]
    pub fn core(
        &self,
        gpu: &B,
        d: &Dims,
        ws: &mut Workspace<B>,
        proj: &B::F32,
        t: usize,
        state: &mut GdnState<B>,
        checkpoint: bool,
        probe: &mut dyn Probe,
    ) -> Result<B::F32> {
        let w = self;
        let (cd, vd, hv) = (d.conv_dim(), d.value_dim(), d.v_heads);
        let n = cd + vd + 2 * hv;
        let (z_off, b_off, a_off) = (cd, cd + vd, cd + vd + hv);
        let mut ckpt = None;
        if checkpoint {
            let old = state.ckpt.take();
            let (c, r, p, q, g, b) = match old {
                Some(c) => (
                    Some(c.conv),
                    Some(c.recurrent),
                    Some(c.proj),
                    Some(c.qkv),
                    Some(c.g),
                    Some(c.beta),
                ),
                None => (None, None, None, None, None, None),
            };
            let mut c = sized(gpu, c, state.conv.len())?;
            let mut r = sized(gpu, r, state.recurrent.len())?;
            gpu.copy_at(&state.conv, &mut c, 0, state.conv.len())?;
            gpu.copy_at(&state.recurrent, &mut r, 0, state.recurrent.len())?;
            ckpt = Some(Checkpoint {
                conv: c,
                recurrent: r,
                proj: sized(gpu, p, t * n)?,
                qkv: sized(gpu, q, t * cd)?,
                g: sized(gpu, g, t * hv)?,
                beta: sized(gpu, b, t * hv)?,
                t,
            });
        } else {
            state.ckpt = None;
        }
        let mut conv = ws.take(gpu, "gdn.conv", t * cd)?;
        gpu.causal_conv_silu(
            proj,
            0,
            n,
            &mut state.conv,
            &w.conv,
            &mut conv,
            t,
            cd,
            d.conv_kernel,
        )?;
        tap(gpu, probe, "gdn.conv_out", &mut conv)?;
        // q and k heads are contiguous at the start of each row: normalise both at once.
        gpu.l2norm_heads(&mut conv, t, cd, 0, 2 * d.k_heads, d.head_k, 1e-6)?;
        let mut g = ws.take(gpu, "gdn.g", t * hv)?;
        let mut beta = ws.take(gpu, "gdn.beta", t * hv)?;
        gpu.gdn_gates(
            proj, a_off, b_off, n, &w.a_log, &w.dt_bias, &mut g, &mut beta, t, hv,
        )?;
        if let Some(c) = ckpt.as_mut() {
            gpu.copy_at(proj, &mut c.proj, 0, t * n)?;
            gpu.copy_at(&conv, &mut c.qkv, 0, t * cd)?;
            gpu.copy_at(&g, &mut c.g, 0, t * hv)?;
            gpu.copy_at(&beta, &mut c.beta, 0, t * hv)?;
        }
        state.ckpt = ckpt;
        let mut core = ws.take(gpu, "gdn.core", t * vd)?;
        gpu.gdn_recurrent(
            &conv,
            &g,
            &beta,
            &mut state.recurrent,
            &mut core,
            t,
            cd,
            d.k_heads,
            hv,
            d.head_k,
            d.head_v,
        )?;
        ws.give("gdn.conv", conv);
        ws.give("gdn.g", g);
        ws.give("gdn.beta", beta);
        tap(gpu, probe, "gdn.core_out", &mut core)?;
        let mut normed = ws.take(gpu, "gdn.normed", t * vd)?;
        gpu.gated_rmsnorm_sigmoid(
            &core,
            proj,
            z_off,
            n,
            hv,
            &w.norm,
            &mut normed,
            t * hv,
            d.head_v,
            d.eps,
        )?;
        ws.give("gdn.core", core);
        Ok(normed)
    }

    /// Returns the state to just after the first `keep` rows of the last
    /// checkpointed step (`1 <= keep <= t`).
    pub fn rewind(
        &self,
        gpu: &B,
        d: &Dims,
        ws: &mut Workspace<B>,
        state: &mut GdnState<B>,
        keep: usize,
    ) -> Result<()> {
        let c = state
            .ckpt
            .as_ref()
            .context("GDN rewind without a checkpointed step")?;
        ensure!(
            keep >= 1 && keep <= c.t,
            "GDN rewind to {keep} of {} rows",
            c.t
        );
        let (cd, vd, hv) = (d.conv_dim(), d.value_dim(), d.v_heads);
        let n = cd + vd + 2 * hv;
        let (nc, nr) = (state.conv.len(), state.recurrent.len());
        gpu.copy_at(&c.conv, &mut state.conv, 0, nc)?;
        gpu.copy_at(&c.recurrent, &mut state.recurrent, 0, nr)?;
        let mut conv_out = ws.take(gpu, "gdn.conv", keep * cd)?;
        gpu.causal_conv_silu(
            &c.proj,
            0,
            n,
            &mut state.conv,
            &self.conv,
            &mut conv_out,
            keep,
            cd,
            d.conv_kernel,
        )?;
        ws.give("gdn.conv", conv_out);
        let mut core = ws.take(gpu, "gdn.core", keep * vd)?;
        gpu.gdn_recurrent(
            &c.qkv,
            &c.g,
            &c.beta,
            &mut state.recurrent,
            &mut core,
            keep,
            cd,
            d.k_heads,
            hv,
            d.head_k,
            d.head_v,
        )?;
        ws.give("gdn.core", core);
        Ok(())
    }
}
