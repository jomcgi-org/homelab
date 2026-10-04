//! Gated DeltaNet, the token mixer of linear-attention layers.

use anyhow::Result;
use oominf_core::{Backend, Probe, Workspace, tap, tap_cols};
use oominf_format::Model;

use crate::Dims;
use crate::util::{bf16_concat, bf16_tensor};

pub struct Gdn<B: Backend> {
    /// `in_proj_qkv`, `in_proj_z`, `in_proj_b`, `in_proj_a` stacked: one GEMM.
    in_proj: B::Bf16,
    conv: B::Bf16,
    a_log: B::Bf16,
    dt_bias: B::Bf16,
    norm: B::Bf16,
    out: B::Bf16,
}

/// Recurrent GDN state carried across steps.
pub struct GdnState<B: Backend> {
    /// `[conv_dim, conv_kernel]` last inputs, oldest first.
    pub conv: B::F32,
    /// `[v_heads, head_k, head_v]`.
    pub recurrent: B::F32,
}

impl<B: Backend> GdnState<B> {
    pub fn new(gpu: &B, d: &Dims) -> Result<Self> {
        Ok(GdnState {
            conv: gpu.zeros(d.conv_dim() * d.conv_kernel)?,
            recurrent: gpu.zeros(d.v_heads * d.head_k * d.head_v)?,
        })
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
            in_proj: bf16_concat(
                gpu,
                model,
                &[
                    (format!("{la}in_proj_qkv.weight"), vec![2 * kd + vd, h]),
                    (format!("{la}in_proj_z.weight"), vec![vd, h]),
                    (format!("{la}in_proj_b.weight"), vec![hv, h]),
                    (format!("{la}in_proj_a.weight"), vec![hv, h]),
                ],
            )?,
            conv: w("conv1d.weight", &[cd, d.conv_kernel as u64])?,
            a_log: w("A_log", &[hv])?,
            dt_bias: w("dt_bias", &[hv])?,
            norm: w("norm.weight", &[d.head_v as u64])?,
            out: w("out_proj.weight", &[h, vd])?,
        })
    }

    /// Returns the mixer output `[t, hidden]` as workspace buffer `gdn.out`; the
    /// caller gives it back.
    #[allow(clippy::too_many_arguments)]
    pub fn forward(
        &self,
        gpu: &B,
        d: &Dims,
        ws: &mut Workspace<B>,
        x: &B::F32,
        t: usize,
        state: &mut GdnState<B>,
        scratch: &mut B::Bf16,
        probe: &mut dyn Probe,
    ) -> Result<B::F32> {
        let w = self;
        let (cd, vd, hv, h) = (d.conv_dim(), d.value_dim(), d.v_heads, d.hidden);
        let n = cd + vd + 2 * hv;
        let mut proj = ws.take(gpu, "gdn.proj", t * n)?;
        gpu.gemm_bf16(x, &w.in_proj, &mut proj, scratch, t, n, h)?;
        // Downstream kernels read the fused projection in place:
        // [qkv (cd) | z (vd) | b (hv) | a (hv)] per row.
        let (z_off, b_off, a_off) = (cd, cd + vd, cd + vd + hv);
        tap_cols(gpu, probe, "gdn.in_proj_qkv", &mut proj, t, n, 0, cd)?;
        tap_cols(gpu, probe, "gdn.in_proj_z", &mut proj, t, n, z_off, vd)?;
        tap_cols(gpu, probe, "gdn.in_proj_b", &mut proj, t, n, b_off, hv)?;
        tap_cols(gpu, probe, "gdn.in_proj_a", &mut proj, t, n, a_off, hv)?;

        let mut conv = ws.take(gpu, "gdn.conv", t * cd)?;
        gpu.causal_conv_silu(
            &proj,
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
            &proj, a_off, b_off, n, &w.a_log, &w.dt_bias, &mut g, &mut beta, t, hv,
        )?;
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
            &proj,
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
        ws.give("gdn.proj", proj);
        tap(gpu, probe, "gdn.norm_out", &mut normed)?;
        let mut out = ws.take(gpu, "gdn.out", t * h)?;
        gpu.gemm_bf16(&normed, &w.out, &mut out, scratch, t, h, vd)?;
        ws.give("gdn.normed", normed);
        Ok(out)
    }
}
