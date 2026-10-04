//! Gated DeltaNet, the token mixer of linear-attention layers.

use anyhow::Result;
use oominf_cuda::{Bf16Buf, Buf, Gpu};
use oominf_format::Model;

use crate::util::{bf16_tensor, tap};
use crate::{Dims, Probe};

pub struct Gdn {
    qkv: Bf16Buf,
    z: Bf16Buf,
    a: Bf16Buf,
    b: Bf16Buf,
    conv: Bf16Buf,
    a_log: Bf16Buf,
    dt_bias: Bf16Buf,
    norm: Bf16Buf,
    out: Bf16Buf,
}

/// Recurrent GDN state carried across steps.
pub struct GdnState {
    /// `[conv_dim, conv_kernel]` last inputs, oldest first.
    pub conv: Buf,
    /// `[v_heads, head_k, head_v]`.
    pub recurrent: Buf,
}

impl GdnState {
    pub fn new(gpu: &Gpu, d: &Dims) -> Result<Self> {
        Ok(GdnState {
            conv: gpu.zeros(d.conv_dim() * d.conv_kernel)?,
            recurrent: gpu.zeros(d.v_heads * d.head_k * d.head_v)?,
        })
    }
}

impl Gdn {
    pub fn load(gpu: &Gpu, model: &Model, d: &Dims, layer: u32) -> Result<Self> {
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
            qkv: w("in_proj_qkv.weight", &[2 * kd + vd, h])?,
            z: w("in_proj_z.weight", &[vd, h])?,
            a: w("in_proj_a.weight", &[hv, h])?,
            b: w("in_proj_b.weight", &[hv, h])?,
            conv: w("conv1d.weight", &[cd, d.conv_kernel as u64])?,
            a_log: w("A_log", &[hv])?,
            dt_bias: w("dt_bias", &[hv])?,
            norm: w("norm.weight", &[d.head_v as u64])?,
            out: w("out_proj.weight", &[h, vd])?,
        })
    }

    #[allow(clippy::too_many_arguments)]
    pub fn forward(
        &self,
        gpu: &Gpu,
        d: &Dims,
        x: &Buf,
        t: usize,
        state: &mut GdnState,
        scratch: &mut Bf16Buf,
        probe: &mut dyn Probe,
    ) -> Result<Buf> {
        let w = self;
        let (cd, vd, hv, h) = (d.conv_dim(), d.value_dim(), d.v_heads, d.hidden);
        let mut qkv = gpu.zeros(t * cd)?;
        gpu.gemm_bf16(x, &w.qkv, &mut qkv, scratch, t, cd, h)?;
        let mut z = gpu.zeros(t * vd)?;
        gpu.gemm_bf16(x, &w.z, &mut z, scratch, t, vd, h)?;
        let mut b = gpu.zeros(t * hv)?;
        gpu.gemm_bf16(x, &w.b, &mut b, scratch, t, hv, h)?;
        let mut a = gpu.zeros(t * hv)?;
        gpu.gemm_bf16(x, &w.a, &mut a, scratch, t, hv, h)?;
        tap(gpu, probe, "gdn.in_proj_qkv", &mut qkv)?;
        tap(gpu, probe, "gdn.in_proj_z", &mut z)?;
        tap(gpu, probe, "gdn.in_proj_b", &mut b)?;
        tap(gpu, probe, "gdn.in_proj_a", &mut a)?;

        let mut conv = gpu.zeros(t * cd)?;
        gpu.causal_conv_silu(
            &qkv,
            &mut state.conv,
            &w.conv,
            &mut conv,
            t,
            cd,
            d.conv_kernel,
        )?;
        tap(gpu, probe, "gdn.conv_out", &mut conv)?;
        let kd = d.key_dim();
        gpu.l2norm_heads(&mut conv, t, cd, 0, d.k_heads, d.head_k, 1e-6)?;
        gpu.l2norm_heads(&mut conv, t, cd, kd, d.k_heads, d.head_k, 1e-6)?;
        let mut g = gpu.zeros(t * hv)?;
        let mut beta = gpu.zeros(t * hv)?;
        gpu.gdn_gates(&a, &b, &w.a_log, &w.dt_bias, &mut g, &mut beta, t, hv)?;
        let mut core = gpu.zeros(t * vd)?;
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
        tap(gpu, probe, "gdn.core_out", &mut core)?;
        let mut normed = gpu.zeros(t * vd)?;
        gpu.gated_rmsnorm_sigmoid(&core, &z, &w.norm, &mut normed, t * hv, d.head_v, d.eps)?;
        tap(gpu, probe, "gdn.norm_out", &mut normed)?;
        let mut out = gpu.zeros(t * h)?;
        gpu.gemm_bf16(&normed, &w.out, &mut out, scratch, t, h, vd)?;
        Ok(out)
    }
}
