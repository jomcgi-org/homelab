//! Hyper-connections: mix the `hc` residual streams down to one block input, and
//! combine a block's output back into every stream.

use anyhow::Result;
use oominf_cuda::{Bf16Buf, Buf, Gpu};
use oominf_format::Model;

use crate::util::{bf16_tensor, tap};
use crate::{Dims, Probe};

pub struct HyperConn {
    norm: Bf16Buf,
    down: Bf16Buf,
    up: Bf16Buf,
    /// `None` for the final mixer, which has no combine.
    inject: Option<Bf16Buf>,
}

impl HyperConn {
    /// Loads `{prefix}.hc_norm.weight` etc.; `combine` loads `block_inject_weight`.
    pub fn load(gpu: &Gpu, model: &Model, d: &Dims, prefix: &str, combine: bool) -> Result<Self> {
        let r = d.residual() as u64;
        let lr = d.hc_lowrank as u64;
        Ok(HyperConn {
            norm: bf16_tensor(gpu, model, &format!("{prefix}.hc_norm.weight"), &[r])?,
            down: bf16_tensor(
                gpu,
                model,
                &format!("{prefix}.input_mix_weight_down.weight"),
                &[lr, r],
            )?,
            up: bf16_tensor(
                gpu,
                model,
                &format!("{prefix}.input_mix_weight_up.weight"),
                &[r, lr],
            )?,
            inject: if combine {
                Some(bf16_tensor(
                    gpu,
                    model,
                    &format!("{prefix}.block_inject_weight.weight"),
                    &[d.hc as u64, r],
                )?)
            } else {
                None
            },
        })
    }

    /// Returns the block input `[t, hidden]` and, with combine, the injection weights
    /// `[t, hc]`. Stages are tapped as `{name}.mixed` and `{name}.inject`.
    #[allow(clippy::too_many_arguments)]
    pub fn mix(
        &self,
        gpu: &Gpu,
        d: &Dims,
        residual: &Buf,
        t: usize,
        scratch: &mut Bf16Buf,
        probe: &mut dyn Probe,
        name: &str,
    ) -> Result<(Buf, Option<Buf>)> {
        let r = d.residual();
        let mut normed = gpu.zeros(t * r)?;
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
        let inv_c = 1.0 / d.hc as f32;
        let mut down = gpu.zeros(t * d.hc_lowrank)?;
        gpu.gemm_bf16(&normed, &self.down, &mut down, scratch, t, d.hc_lowrank, r)?;
        let mut act = gpu.zeros(t * d.hc_lowrank)?;
        gpu.silu_scale(&down, &mut act, inv_c, t * d.hc_lowrank)?;
        let mut up = gpu.zeros(t * r)?;
        gpu.gemm_bf16(&act, &self.up, &mut up, scratch, t, r, d.hc_lowrank)?;
        let mut mixed = gpu.zeros(t * d.hidden)?;
        gpu.hc_mix(&up, &normed, &mut mixed, t, d.hc, d.hidden)?;
        tap(gpu, probe, &format!("{name}.mixed"), &mut mixed)?;
        let Some(w) = &self.inject else {
            return Ok((mixed, None));
        };
        let mut logit = gpu.zeros(t * d.hc)?;
        gpu.gemm_bf16(&normed, w, &mut logit, scratch, t, d.hc, r)?;
        let mut inject = gpu.zeros(t * d.hc)?;
        gpu.hc_inject(&logit, &mut inject, t * d.hc, inv_c)?;
        tap(gpu, probe, &format!("{name}.inject"), &mut inject)?;
        Ok((mixed, Some(inject)))
    }

    /// `residual + block_out (x) inject`, broadcast over the streams.
    pub fn combine(
        gpu: &Gpu,
        d: &Dims,
        residual: &Buf,
        block_out: &Buf,
        inject: &Buf,
        t: usize,
    ) -> Result<Buf> {
        let mut out = gpu.zeros(t * d.residual())?;
        gpu.hc_combine(residual, block_out, inject, &mut out, t, d.hc, d.hidden)?;
        Ok(out)
    }
}
