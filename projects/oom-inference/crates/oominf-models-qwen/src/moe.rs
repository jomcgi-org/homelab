//! Sparse MoE: router, shared expert and NVFP4 routed experts (W4A16 in fp32).

use std::collections::BTreeMap;
use std::sync::Arc;

use anyhow::{Context, Result, bail, ensure};
use oominf_cuda::{Bf16Buf, Buf, Gpu, Slice};
use oominf_format::Model;

use crate::util::{bf16_tensor, tap};
use crate::{Dims, Probe};

/// Byte offsets of one projection's parts inside an expert record.
#[derive(Clone, Copy)]
struct ProjParts {
    weight: usize,
    scale: usize,
    rows: usize,
    cols: usize,
}

/// Supplies routed-expert records on the device. The tiering engine implements this
/// with VRAM slots, a host tier and disk; [`DiskExperts`] reads straight from the
/// model files.
pub trait ExpertSource {
    /// Makes `experts` of `layer` device-resident and returns the raw device address
    /// of each one's record, in the same order. Addresses stay valid until the next
    /// `fetch` call. Kernels read every part, `weight_scale_2` included, from the
    /// record itself.
    fn fetch(&mut self, gpu: &Gpu, layer: u32, experts: &[u32]) -> Result<Vec<u64>>;
}

/// Reads every requested record from disk and uploads it (no caching).
pub struct DiskExperts {
    model: Arc<Model>,
    host: Vec<u8>,
    held: Vec<Slice<u8>>,
}

impl DiskExperts {
    pub fn new(model: Arc<Model>) -> Self {
        DiskExperts {
            model,
            host: Vec::new(),
            held: Vec::new(),
        }
    }
}

impl ExpertSource for DiskExperts {
    fn fetch(&mut self, gpu: &Gpu, layer: u32, experts: &[u32]) -> Result<Vec<u64>> {
        self.held.clear();
        for &expert in experts {
            let (_, stride) = self.model.record_location(layer, expert)?;
            self.host.resize(stride as usize, 0);
            self.model.read_record(layer, expert, &mut self.host)?;
            self.held.push(gpu.upload_bytes(&self.host)?);
        }
        Ok(self.held.iter().map(|b| gpu.device_ptr(b)).collect())
    }
}

/// Index of each projection's `weight_scale_2` in the record's leading scalars part
/// (gate.ws2, gate.in, up.ws2, up.in, down.ws2, down.in).
const SCALE2_IDX: [usize; 3] = [0, 2, 4];

pub struct Moe {
    layer: u32,
    router: Bf16Buf,
    shared_gate: Bf16Buf,
    shared_up: Bf16Buf,
    shared_down: Bf16Buf,
    shared_gate_logit: Bf16Buf,
    gate: ProjParts,
    up: ProjParts,
    down: ProjParts,
}

impl Moe {
    pub fn load(gpu: &Gpu, model: &Model, d: &Dims, layer: u32) -> Result<Self> {
        let p = format!("model.language_model.layers.{layer}.mlp.");
        let h = d.hidden as u64;
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
        let proj = |name: &str| -> Result<ProjParts> {
            let part = |n: &str| {
                group
                    .schema
                    .part(n)
                    .with_context(|| format!("expert record has no part {n}"))
            };
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
        let si = d.shared_inter as u64;
        let w = |n: &str, s: &[u64]| bf16_tensor(gpu, model, &format!("{p}{n}"), s);
        Ok(Moe {
            layer,
            router: w("gate.weight", &[d.experts as u64, h])?,
            shared_gate: w("shared_expert.gate_proj.weight", &[si, h])?,
            shared_up: w("shared_expert.up_proj.weight", &[si, h])?,
            shared_down: w("shared_expert.down_proj.weight", &[h, si])?,
            shared_gate_logit: w("shared_expert_gate.weight", &[1, h])?,
            gate,
            up,
            down,
        })
    }

    #[allow(clippy::too_many_arguments)]
    pub fn forward(
        &self,
        gpu: &Gpu,
        d: &Dims,
        x: &Buf,
        t: usize,
        experts: &mut dyn ExpertSource,
        scratch: &mut Bf16Buf,
        probe: &mut dyn Probe,
    ) -> Result<Buf> {
        let (h, e, k) = (d.hidden, d.experts, d.top_k);

        let mut logits = gpu.zeros(t * e)?;
        gpu.gemm_bf16(x, &self.router, &mut logits, scratch, t, e, h)?;
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
        gpu.gemm_bf16(x, &self.shared_gate, &mut sg, scratch, t, si, h)?;
        let mut su = gpu.zeros(t * si)?;
        gpu.gemm_bf16(x, &self.shared_up, &mut su, scratch, t, si, h)?;
        let mut sact = gpu.zeros(t * si)?;
        gpu.silu_mul(&sg, &su, &mut sact, t * si)?;
        let mut shared = gpu.zeros(t * h)?;
        gpu.gemm_bf16(&sact, &self.shared_down, &mut shared, scratch, t, h, si)?;
        tap(gpu, probe, "shared_out", &mut shared)?;
        let mut gate_logit = gpu.zeros(t)?;
        gpu.gemm_bf16(
            x,
            &self.shared_gate_logit,
            &mut gate_logit,
            scratch,
            t,
            1,
            h,
        )?;
        tap(gpu, probe, "shared_gate_logit", &mut gate_logit)?;

        // Routed experts, grouped by expert.
        let mut by_expert: BTreeMap<u32, (Vec<i32>, Vec<f32>)> = BTreeMap::new();
        for tok in 0..t {
            for slot in 0..k {
                let ex = ids_host[tok * k + slot];
                if ex < 0 || ex as usize >= e {
                    bail!("router picked expert {ex}");
                }
                let entry = by_expert.entry(ex as u32).or_default();
                entry.0.push(tok as i32);
                entry.1.push(weights_host[tok * k + slot]);
            }
        }
        let (gi, gh) = (self.gate.rows, self.gate.cols);
        let (dr, dc) = (self.down.rows, self.down.cols);
        let mut w_gate = gpu.zeros(gi * gh)?;
        let mut w_up = gpu.zeros(gi * gh)?;
        let mut w_down = gpu.zeros(dr * dc)?;
        let mut routed = gpu.zeros(t * h)?;
        let distinct: Vec<u32> = by_expert.keys().copied().collect();
        let records = experts.fetch(gpu, self.layer, &distinct)?;
        for ((toks, wts), &rec) in by_expert.values().zip(&records) {
            let n = toks.len();
            let (g_, u_, d_) = (self.gate, self.up, self.down);
            let [s_gate, s_up, s_down] = SCALE2_IDX;
            gpu.dequant_nvfp4(rec, g_.weight, g_.scale, s_gate, &mut w_gate, gi, gh)?;
            gpu.dequant_nvfp4(rec, u_.weight, u_.scale, s_up, &mut w_up, gi, gh)?;
            gpu.dequant_nvfp4(rec, d_.weight, d_.scale, s_down, &mut w_down, dr, dc)?;
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
