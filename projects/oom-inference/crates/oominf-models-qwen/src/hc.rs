//! Hyper-connections: mix the `hc` residual streams down to one block input, and
//! combine a block's output back into every stream.

use anyhow::Result;
use oominf_core::{Backend, Probe, Workspace, tap};
use oominf_format::Model;

use crate::Dims;
use crate::util::{bf16_concat, bf16_tensor};

pub struct HyperConn<B: Backend> {
    norm: B::Bf16,
    /// `input_mix_weight_down` with, when the connection combines,
    /// `block_inject_weight` stacked under it: both read the normed residual.
    down_inject: B::Bf16,
    up: B::Bf16,
    combine: bool,
}

impl<B: Backend> HyperConn<B> {
    /// Loads `{prefix}.hc_norm.weight` etc.; `combine` loads `block_inject_weight`.
    pub fn load(gpu: &B, model: &Model, d: &Dims, prefix: &str, combine: bool) -> Result<Self> {
        let r = d.residual() as u64;
        let lr = d.hc_lowrank as u64;
        let mut parts = vec![(
            format!("{prefix}.input_mix_weight_down.weight"),
            vec![lr, r],
        )];
        if combine {
            parts.push((
                format!("{prefix}.block_inject_weight.weight"),
                vec![d.hc as u64, r],
            ));
        }
        Ok(HyperConn {
            norm: bf16_tensor(gpu, model, &format!("{prefix}.hc_norm.weight"), &[r])?,
            down_inject: bf16_concat(gpu, model, &parts)?,
            up: bf16_tensor(
                gpu,
                model,
                &format!("{prefix}.input_mix_weight_up.weight"),
                &[r, lr],
            )?,
            combine,
        })
    }

    /// Returns the block input `[t, hidden]` (workspace buffer `hc.mixed`) and, with
    /// combine, the injection weights `[t, hc]` (`hc.inject`). The caller gives both
    /// back to `ws`. Stages are tapped as `{name}.mixed` and `{name}.inject`.
    #[allow(clippy::too_many_arguments)]
    pub fn mix(
        &self,
        gpu: &B,
        d: &Dims,
        ws: &mut Workspace<B>,
        residual: &B::F32,
        t: usize,
        scratch: &mut B::Bf16,
        probe: &mut dyn Probe,
        name: &str,
    ) -> Result<(B::F32, Option<B::F32>)> {
        let r = d.residual();
        let lr = d.hc_lowrank;
        let hc = if self.combine { d.hc } else { 0 };
        let inv_c = 1.0 / d.hc as f32;
        let mut normed = ws.take(gpu, "hc.normed", t * r)?;
        gpu.rmsnorm_groups(
            residual,
            &self.norm,
            &mut normed,
            t,
            r,
            d.hidden,
            d.eps,
            1.0,
        )?;
        let mut down = ws.take(gpu, "hc.down", t * (lr + hc))?;
        gpu.gemm_bf16(
            &normed,
            &self.down_inject,
            &mut down,
            scratch,
            t,
            lr + hc,
            r,
        )?;
        let mut act = ws.take(gpu, "hc.act", t * lr)?;
        let mut inject = ws.take(gpu, "hc.inject", t * hc.max(1))?;
        if self.combine {
            gpu.hc_post_down(&down, lr, hc, inv_c, &mut act, &mut inject, t)?;
        } else {
            gpu.silu_scale(&down, &mut act, inv_c, t * lr)?;
        }
        ws.give("hc.down", down);
        let mut up = ws.take(gpu, "hc.up", t * r)?;
        gpu.gemm_bf16(&act, &self.up, &mut up, scratch, t, r, lr)?;
        ws.give("hc.act", act);
        let mut mixed = ws.take(gpu, "hc.mixed", t * d.hidden)?;
        gpu.hc_mix(&up, &normed, &mut mixed, t, d.hc, d.hidden)?;
        ws.give("hc.up", up);
        ws.give("hc.normed", normed);
        tap(gpu, probe, &format!("{name}.mixed"), &mut mixed)?;
        if !self.combine {
            ws.give("hc.inject", inject);
            return Ok((mixed, None));
        }
        tap(gpu, probe, &format!("{name}.inject"), &mut inject)?;
        Ok((mixed, Some(inject)))
    }

    /// `out = residual + block_out (x) inject`, broadcast over the streams.
    pub fn combine_into(
        gpu: &B,
        d: &Dims,
        residual: &B::F32,
        block_out: &B::F32,
        inject: &B::F32,
        t: usize,
        out: &mut B::F32,
    ) -> Result<()> {
        gpu.hc_combine(residual, block_out, inject, out, t, d.hc, d.hidden)?;
        Ok(())
    }
}
