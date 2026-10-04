//! A linear-attention decoder layer: hyper-connections around Gated DeltaNet and
//! the routed + shared expert MoE.

use std::collections::BTreeMap;

use anyhow::{Context, Result, bail, ensure};
use oominf_cuda::{Bf16Buf, Buf, Gpu, Slice};
use oominf_format::Model;

use crate::{Dims, Probe};

struct HyperConn {
    norm: Bf16Buf,
    down: Bf16Buf,
    up: Bf16Buf,
    inject: Bf16Buf,
}

struct Gdn {
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

/// Byte offsets of one projection's parts inside an expert record.
#[derive(Clone, Copy)]
struct ProjParts {
    weight: usize,
    scale: usize,
    rows: usize,
    cols: usize,
}

struct Moe {
    router: Bf16Buf,
    shared_gate: Bf16Buf,
    shared_up: Bf16Buf,
    shared_down: Bf16Buf,
    shared_gate_logit: Bf16Buf,
    /// The layer's expert records, device resident, `stride` bytes each.
    records: Slice<u8>,
    stride: usize,
    gate: ProjParts,
    up: ProjParts,
    down: ProjParts,
    /// Per expert: [gate, up, down] `weight_scale_2`.
    scale2: Vec<[f32; 3]>,
}

pub struct LinearLayer {
    pub layer: u32,
    dims: Dims,
    attn_hc: HyperConn,
    mlp_hc: HyperConn,
    gdn: Gdn,
    moe: Moe,
}

/// Recurrent GDN state carried across steps.
pub struct GdnState {
    /// `[conv_dim, conv_kernel]` last inputs, oldest first.
    pub conv: Buf,
    /// `[v_heads, head_k, head_v]`.
    pub recurrent: Buf,
}

impl GdnState {
    pub fn new(gpu: &Gpu, dims: &Dims) -> Result<Self> {
        Ok(GdnState {
            conv: gpu.zeros(dims.conv_dim() * dims.conv_kernel)?,
            recurrent: gpu.zeros(dims.v_heads * dims.head_k * dims.head_v)?,
        })
    }
}

fn bf16_tensor(gpu: &Gpu, model: &Model, name: &str, shape: &[u64]) -> Result<Bf16Buf> {
    let t = model
        .tensor(name)
        .with_context(|| format!("missing tensor {name}"))?;
    ensure!(t.dtype == "BF16", "{name}: expected BF16, got {}", t.dtype);
    let want: u64 = shape.iter().product();
    ensure!(
        t.shape.iter().product::<u64>() == want,
        "{name}: shape {:?}, expected {shape:?}",
        t.shape
    );
    let bytes = model.read_tensor(t)?;
    let words: Vec<u16> = bytes
        .as_chunks::<2>()
        .0
        .iter()
        .map(|&c| u16::from_le_bytes(c))
        .collect();
    Ok(gpu.upload_u16(&words)?)
}

impl LinearLayer {
    pub fn load(gpu: &Gpu, model: &Model, dims: &Dims, layer: u32) -> Result<Self> {
        let d = dims;
        let p = format!("model.language_model.layers.{layer}.");
        let (h, r, c) = (d.hidden as u64, d.residual() as u64, d.hc as u64);
        let hc = |which: &str| -> Result<HyperConn> {
            Ok(HyperConn {
                norm: bf16_tensor(gpu, model, &format!("{p}{which}.hc_norm.weight"), &[r])?,
                down: bf16_tensor(
                    gpu,
                    model,
                    &format!("{p}{which}.input_mix_weight_down.weight"),
                    &[d.hc_lowrank as u64, r],
                )?,
                up: bf16_tensor(
                    gpu,
                    model,
                    &format!("{p}{which}.input_mix_weight_up.weight"),
                    &[r, d.hc_lowrank as u64],
                )?,
                inject: bf16_tensor(
                    gpu,
                    model,
                    &format!("{p}{which}.block_inject_weight.weight"),
                    &[c, r],
                )?,
            })
        };
        let la = format!("{p}linear_attn.");
        let (kd, vd, cd, hv) = (
            d.key_dim() as u64,
            d.value_dim() as u64,
            d.conv_dim() as u64,
            d.v_heads as u64,
        );
        let gdn = Gdn {
            qkv: bf16_tensor(
                gpu,
                model,
                &format!("{la}in_proj_qkv.weight"),
                &[2 * kd + vd, h],
            )?,
            z: bf16_tensor(gpu, model, &format!("{la}in_proj_z.weight"), &[vd, h])?,
            a: bf16_tensor(gpu, model, &format!("{la}in_proj_a.weight"), &[hv, h])?,
            b: bf16_tensor(gpu, model, &format!("{la}in_proj_b.weight"), &[hv, h])?,
            conv: bf16_tensor(
                gpu,
                model,
                &format!("{la}conv1d.weight"),
                &[cd, d.conv_kernel as u64],
            )?,
            a_log: bf16_tensor(gpu, model, &format!("{la}A_log"), &[hv])?,
            dt_bias: bf16_tensor(gpu, model, &format!("{la}dt_bias"), &[hv])?,
            norm: bf16_tensor(gpu, model, &format!("{la}norm.weight"), &[d.head_v as u64])?,
            out: bf16_tensor(gpu, model, &format!("{la}out_proj.weight"), &[h, vd])?,
        };

        let group = model
            .expert_group(layer)
            .with_context(|| format!("no experts for layer {layer}"))?;
        ensure!(
            group.schema.layout == "nvfp4-modelopt-g16",
            "unsupported expert layout {}",
            group.schema.layout
        );
        ensure!(
            group.num_experts as usize == d.experts,
            "layer {layer} has {} experts",
            group.num_experts
        );
        let part = |name: &str| -> Result<&oominf_format::Part> {
            group
                .schema
                .part(name)
                .with_context(|| format!("expert record has no part {name}"))
        };
        let proj = |name: &str| -> Result<ProjParts> {
            let w = part(&format!("{name}.weight"))?;
            let s = part(&format!("{name}.weight_scale"))?;
            Ok(ProjParts {
                weight: w.offset as usize,
                scale: s.offset as usize,
                rows: w.shape[0] as usize,
                cols: 2 * w.shape[1] as usize,
            })
        };
        let (gate, up, down) = (proj("gate")?, proj("up")?, proj("down")?);
        ensure!(
            gate.rows == d.moe_inter && gate.cols == d.hidden && down.rows == d.hidden,
            "expert shapes do not match config"
        );
        let stride = group.schema.stride as usize;
        let mut host = vec![0u8; stride * d.experts];
        let mut scale2 = Vec::with_capacity(d.experts);
        for e in 0..d.experts {
            let rec = &mut host[e * stride..(e + 1) * stride];
            model.read_record(layer, e as u32, rec)?;
            let s = |i: usize| f32::from_le_bytes(rec[i * 4..i * 4 + 4].try_into().unwrap());
            // scalars: gate.ws2, gate.in, up.ws2, up.in, down.ws2, down.in
            scale2.push([s(0), s(2), s(4)]);
        }
        let moe = Moe {
            router: bf16_tensor(
                gpu,
                model,
                &format!("{p}mlp.gate.weight"),
                &[d.experts as u64, h],
            )?,
            shared_gate: bf16_tensor(
                gpu,
                model,
                &format!("{p}mlp.shared_expert.gate_proj.weight"),
                &[d.shared_inter as u64, h],
            )?,
            shared_up: bf16_tensor(
                gpu,
                model,
                &format!("{p}mlp.shared_expert.up_proj.weight"),
                &[d.shared_inter as u64, h],
            )?,
            shared_down: bf16_tensor(
                gpu,
                model,
                &format!("{p}mlp.shared_expert.down_proj.weight"),
                &[h, d.shared_inter as u64],
            )?,
            shared_gate_logit: bf16_tensor(
                gpu,
                model,
                &format!("{p}mlp.shared_expert_gate.weight"),
                &[1, h],
            )?,
            records: gpu.upload_bytes(&host)?,
            stride,
            gate,
            up,
            down,
            scale2,
        };
        Ok(LinearLayer {
            layer,
            dims: d.clone(),
            attn_hc: hc("attn_hyper_connection")?,
            mlp_hc: hc("mlp_hyper_connection")?,
            gdn,
            moe,
        })
    }

    /// Runs the layer over `t` tokens. `residual` is `[t, hc * hidden]`; returns the
    /// new residual.
    pub fn forward(
        &self,
        gpu: &Gpu,
        residual: &Buf,
        t: usize,
        state: &mut GdnState,
        probe: &mut dyn Probe,
    ) -> Result<Buf> {
        let d = &self.dims;
        let mut scratch = gpu.upload_u16(&vec![0u16; t * d.residual().max(d.conv_dim())])?;

        let (mixed, inject) = self.hyper_mix(
            gpu,
            &self.attn_hc,
            residual,
            t,
            &mut scratch,
            probe,
            "attn_hc",
        )?;
        let mixer_out = self.gdn(gpu, &mixed, t, state, &mut scratch, probe)?;
        let mut res1 = gpu.zeros(t * d.residual())?;
        gpu.hc_combine(residual, &mixer_out, &inject, &mut res1, t, d.hc, d.hidden)?;
        tap(gpu, probe, "attn_combine_out", &mut res1)?;

        let (mixed, inject) =
            self.hyper_mix(gpu, &self.mlp_hc, &res1, t, &mut scratch, probe, "mlp_hc")?;
        let moe_out = self.moe(gpu, &mixed, t, &mut scratch, probe)?;
        let mut out = gpu.zeros(t * d.residual())?;
        gpu.hc_combine(&res1, &moe_out, &inject, &mut out, t, d.hc, d.hidden)?;
        tap(gpu, probe, "layer_out", &mut out)?;
        tap(gpu, probe, "state.conv", &mut state.conv)?;
        tap(gpu, probe, "state.recurrent", &mut state.recurrent)?;
        Ok(out)
    }

    #[allow(clippy::too_many_arguments)]
    fn hyper_mix(
        &self,
        gpu: &Gpu,
        w: &HyperConn,
        residual: &Buf,
        t: usize,
        scratch: &mut Bf16Buf,
        probe: &mut dyn Probe,
        name: &str,
    ) -> Result<(Buf, Buf)> {
        let d = &self.dims;
        let r = d.residual();
        let mut normed = gpu.zeros(t * r)?;
        gpu.rmsnorm_groups(residual, &w.norm, &mut normed, t, r, d.hidden, d.eps, 1.0)?;
        let inv_c = 1.0 / d.hc as f32;
        let mut down = gpu.zeros(t * d.hc_lowrank)?;
        gpu.gemm_bf16(&normed, &w.down, &mut down, scratch, t, d.hc_lowrank, r)?;
        let mut act = gpu.zeros(t * d.hc_lowrank)?;
        gpu.silu_scale(&down, &mut act, inv_c, t * d.hc_lowrank)?;
        let mut up = gpu.zeros(t * r)?;
        gpu.gemm_bf16(&act, &w.up, &mut up, scratch, t, r, d.hc_lowrank)?;
        let mut mixed = gpu.zeros(t * d.hidden)?;
        gpu.hc_mix(&up, &normed, &mut mixed, t, d.hc, d.hidden)?;
        let mut logit = gpu.zeros(t * d.hc)?;
        gpu.gemm_bf16(&normed, &w.inject, &mut logit, scratch, t, d.hc, r)?;
        let mut inject = gpu.zeros(t * d.hc)?;
        gpu.hc_inject(&logit, &mut inject, t * d.hc, inv_c)?;
        tap(gpu, probe, &format!("{name}.mixed"), &mut mixed)?;
        tap(gpu, probe, &format!("{name}.inject"), &mut inject)?;
        Ok((mixed, inject))
    }

    fn gdn(
        &self,
        gpu: &Gpu,
        x: &Buf,
        t: usize,
        state: &mut GdnState,
        scratch: &mut Bf16Buf,
        probe: &mut dyn Probe,
    ) -> Result<Buf> {
        let d = &self.dims;
        let w = &self.gdn;
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
        tap(gpu, probe, "mixer_out", &mut out)?;
        Ok(out)
    }

    fn moe(
        &self,
        gpu: &Gpu,
        x: &Buf,
        t: usize,
        scratch: &mut Bf16Buf,
        probe: &mut dyn Probe,
    ) -> Result<Buf> {
        let d = &self.dims;
        let m = &self.moe;
        let (h, e, k) = (d.hidden, d.experts, d.top_k);

        let mut logits = gpu.zeros(t * e)?;
        gpu.gemm_bf16(x, &m.router, &mut logits, scratch, t, e, h)?;
        tap(gpu, probe, "router_logits", &mut logits)?;
        let mut ids = gpu.upload_i32(&vec![0i32; t * k])?;
        let mut weights = gpu.zeros(t * k)?;
        gpu.router_topk(&logits, &mut ids, &mut weights, t, e, k)?;
        tap(gpu, probe, "topk_weights", &mut weights)?;
        let mut ids_host = gpu.download(&ids)?;
        if probe.wants("topk_ids") {
            probe.observe("topk_ids", ids_host.iter().map(|&i| i as f32).collect());
        }
        if let Some(sub) = probe.substitute("topk_ids") {
            ensure!(
                sub.len() == ids_host.len(),
                "topk_ids substitute has wrong length"
            );
            ids_host = sub.iter().map(|&v| v as i32).collect();
        }
        let weights_host = gpu.download(&weights)?;

        // Shared expert.
        let si = d.shared_inter;
        let mut sg = gpu.zeros(t * si)?;
        gpu.gemm_bf16(x, &m.shared_gate, &mut sg, scratch, t, si, h)?;
        let mut su = gpu.zeros(t * si)?;
        gpu.gemm_bf16(x, &m.shared_up, &mut su, scratch, t, si, h)?;
        let mut sact = gpu.zeros(t * si)?;
        gpu.silu_mul(&sg, &su, &mut sact, t * si)?;
        let mut shared = gpu.zeros(t * h)?;
        gpu.gemm_bf16(&sact, &m.shared_down, &mut shared, scratch, t, h, si)?;
        tap(gpu, probe, "shared_out", &mut shared)?;
        let mut gate_logit = gpu.zeros(t)?;
        gpu.gemm_bf16(x, &m.shared_gate_logit, &mut gate_logit, scratch, t, 1, h)?;
        tap(gpu, probe, "shared_gate_logit", &mut gate_logit)?;

        // Routed experts, grouped by expert.
        let mut by_expert: BTreeMap<usize, (Vec<i32>, Vec<f32>)> = BTreeMap::new();
        for tok in 0..t {
            for slot in 0..k {
                let ex = ids_host[tok * k + slot];
                if ex < 0 || ex as usize >= e {
                    bail!("router picked expert {ex}");
                }
                let entry = by_expert.entry(ex as usize).or_default();
                entry.0.push(tok as i32);
                entry.1.push(weights_host[tok * k + slot]);
            }
        }
        let (gi, gh) = (m.gate.rows, m.gate.cols);
        let mut w_gate = gpu.zeros(gi * gh)?;
        let mut w_up = gpu.zeros(gi * gh)?;
        let mut w_down = gpu.zeros(m.down.rows * m.down.cols)?;
        let mut routed = gpu.zeros(t * h)?;
        for (&ex, (toks, wts)) in &by_expert {
            let n = toks.len();
            let base = ex * m.stride;
            let [s_gate, s_up, s_down] = m.scale2[ex];
            let (g_, u_, d_) = (m.gate, m.up, m.down);
            gpu.dequant_nvfp4(
                &m.records,
                base + g_.weight,
                base + g_.scale,
                s_gate,
                &mut w_gate,
                gi,
                gh,
            )?;
            gpu.dequant_nvfp4(
                &m.records,
                base + u_.weight,
                base + u_.scale,
                s_up,
                &mut w_up,
                gi,
                gh,
            )?;
            gpu.dequant_nvfp4(
                &m.records,
                base + d_.weight,
                base + d_.scale,
                s_down,
                &mut w_down,
                d_.rows,
                d_.cols,
            )?;
            let idx = gpu.upload_i32(toks)?;
            let wv = gpu.upload_f32(wts)?;
            let mut xs = gpu.zeros(n * h)?;
            gpu.gather_rows(x, &idx, &mut xs, n, h)?;
            let mut g = gpu.zeros(n * gi)?;
            gpu.gemm_f32(&xs, &w_gate, &mut g, n, gi, gh)?;
            let mut u = gpu.zeros(n * gi)?;
            gpu.gemm_f32(&xs, &w_up, &mut u, n, gi, gh)?;
            let mut act = gpu.zeros(n * gi)?;
            gpu.silu_mul(&g, &u, &mut act, n * gi)?;
            let mut y = gpu.zeros(n * h)?;
            gpu.gemm_f32(&act, &w_down, &mut y, n, h, gi)?;
            gpu.scatter_add_weighted(&y, &idx, &wv, &mut routed, n, h)?;
        }
        tap(gpu, probe, "routed_out", &mut routed)?;
        let mut out = gpu.zeros(t * h)?;
        gpu.moe_combine(&routed, &shared, &gate_logit, &mut out, t, h)?;
        tap(gpu, probe, "moe_out", &mut out)?;
        Ok(out)
    }
}

/// Reports `buf` to the probe and swaps in a substitute if the probe has one.
fn tap(gpu: &Gpu, probe: &mut dyn Probe, stage: &str, buf: &mut Buf) -> Result<()> {
    if probe.wants(stage) {
        probe.observe(stage, gpu.download(buf)?);
    }
    if let Some(sub) = probe.substitute(stage) {
        ensure!(
            sub.len() == buf.len(),
            "{stage}: substitute has {} values, buffer {}",
            sub.len(),
            buf.len()
        );
        *buf = gpu.upload_f32(&sub)?;
    }
    Ok(())
}
