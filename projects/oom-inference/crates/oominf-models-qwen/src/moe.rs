//! Sparse MoE: router, shared expert and NVFP4 routed experts (W4A16 in fp32).

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use anyhow::{Context, Result, bail, ensure};
use oominf_cuda::{Bf16Buf, Buf, Gpu, Nvfp4Record, Slice, Workspace};
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
///
/// Kernels read every part of a record, `weight_scale_2` included, from the record
/// itself, so a source only hands out record addresses.
pub trait ExpertSource {
    /// Makes `experts` of `layer` device-resident and returns the raw device address
    /// of each one's record, in the same order, usable by work enqueued afterwards.
    /// Addresses stay valid until the next fetch.
    fn fetch(&mut self, gpu: &Gpu, layer: u32, experts: &[u32]) -> Result<Vec<u64>>;

    /// Two-phase fetch: returns every record's address and whether it is usable by
    /// work enqueued now. The rest become usable after [`ExpertSource::finish_fetch`],
    /// so callers can compute with resident experts while the others load.
    fn begin_fetch(&mut self, gpu: &Gpu, layer: u32, experts: &[u32]) -> Result<Staged> {
        let addrs = self.fetch(gpu, layer, experts)?;
        Ok(Staged {
            ready: vec![true; addrs.len()],
            addrs,
        })
    }

    /// Completes the last [`ExpertSource::begin_fetch`]: every record it returned is
    /// usable by work enqueued after this call.
    fn finish_fetch(&mut self, _gpu: &Gpu) -> Result<()> {
        Ok(())
    }
}

/// Result of [`ExpertSource::begin_fetch`].
pub struct Staged {
    pub addrs: Vec<u64>,
    /// `ready[i]`: record `i` is usable before `finish_fetch`.
    pub ready: Vec<bool>,
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

/// Per-step routing tables of the fused path. They are small (a few KiB), so each
/// layer keeps its own; the large activation buffers come from the sequence
/// workspace, shared by every layer.
struct Tables {
    /// `off[n_e + 1] ++ assign_tok[A] ++ slot_assign[A]`.
    meta: Slice<i32>,
    /// Record address of each expert of the step.
    recs: Slice<u64>,
}

/// Steps where some expert has at least this many assignments use the tiled
/// (shared-memory) kernels; smaller steps use the warp-per-row kernels.
const TILED_MIN_ASSIGNMENTS: usize = 32;

pub struct Moe {
    layer: u32,
    geo: Nvfp4Record,
    /// Use the slow reference path (oracle); defaults from `OOMINF_MOE_REFERENCE`.
    reference: AtomicBool,
    tables: Mutex<Tables>,
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
        ensure!(
            down.cols == gate.rows,
            "expert down input does not match gate output"
        );
        let geo = Nvfp4Record {
            hidden: gate.cols,
            inter: gate.rows,
            gate_weight: gate.weight,
            gate_scale: gate.scale,
            up_weight: up.weight,
            up_scale: up.scale,
            down_weight: down.weight,
            down_scale: down.scale,
            scale2: SCALE2_IDX,
        };
        let tables = Tables {
            meta: gpu.upload_i32(&[0; 64])?,
            recs: gpu
                .ctx
                .default_stream()
                .alloc_zeros::<u64>(64)
                .map_err(oominf_cuda::Error::from)?,
        };
        Ok(Moe {
            layer,
            geo,
            reference: AtomicBool::new(
                std::env::var_os("OOMINF_MOE_REFERENCE").is_some_and(|v| v != "0"),
            ),
            tables: Mutex::new(tables),
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

    /// Switches between the fused path and the slow reference path (oracle).
    pub fn set_reference(&self, on: bool) {
        self.reference.store(on, Ordering::Relaxed);
    }

    #[allow(clippy::too_many_arguments)]
    pub fn forward(
        &self,
        gpu: &Gpu,
        d: &Dims,
        ws: &mut Workspace,
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

        // Start loading the routed experts so their copies overlap the shared expert
        // and the resident experts' compute.
        let reference = self.reference.load(Ordering::Relaxed);
        let plan = if reference {
            None
        } else {
            Some(self.begin_routed(gpu, d, t, &ids_host, experts)?)
        };

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

        let mut routed = match plan {
            None => {
                let weights_host = gpu.download(&weights)?;
                self.routed_reference(gpu, d, x, t, &ids_host, &weights_host, experts)?
            }
            Some(plan) => self.finish_routed(gpu, d, ws, x, t, plan, &weights, experts)?,
        };
        tap(gpu, probe, "routed_out", &mut routed)?;
        let mut out = gpu.zeros(t * h)?;
        gpu.moe_combine(&routed, &shared, &gate_logit, &mut out, t, h)?;
        tap(gpu, probe, "moe_out", &mut out)?;
        Ok(out)
    }

    /// Reference routed-expert path: per expert, dequantise to fp32 matrices and run
    /// fp32 cuBLAS GEMMs. Slow; kept as an oracle (`OOMINF_MOE_REFERENCE=1`).
    #[allow(clippy::too_many_arguments)]
    fn routed_reference(
        &self,
        gpu: &Gpu,
        d: &Dims,
        x: &Buf,
        t: usize,
        ids_host: &[i32],
        weights: &[f32],
        experts: &mut dyn ExpertSource,
    ) -> Result<Buf> {
        let (h, e, k) = (d.hidden, d.experts, d.top_k);
        let mut by_expert: BTreeMap<u32, (Vec<i32>, Vec<f32>)> = BTreeMap::new();
        for tok in 0..t {
            for slot in 0..k {
                let ex = ids_host[tok * k + slot];
                if ex < 0 || ex as usize >= e {
                    bail!("router picked expert {ex}");
                }
                let entry = by_expert.entry(ex as u32).or_default();
                entry.0.push(tok as i32);
                entry.1.push(weights[tok * k + slot]);
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
        Ok(routed)
    }

    /// Groups the step's assignments by expert, resident experts first, and starts
    /// fetching their records.
    fn begin_routed(
        &self,
        gpu: &Gpu,
        d: &Dims,
        t: usize,
        ids_host: &[i32],
        experts: &mut dyn ExpertSource,
    ) -> Result<RoutedPlan> {
        let (e, k) = (d.experts, d.top_k);
        // Group assignment slots (t * k + s) by expert.
        let mut by_expert: BTreeMap<u32, Vec<usize>> = BTreeMap::new();
        for (slot, &ex) in ids_host.iter().enumerate().take(t * k) {
            if ex < 0 || ex as usize >= e {
                bail!("router picked expert {ex}");
            }
            by_expert.entry(ex as u32).or_default().push(slot);
        }
        let distinct: Vec<u32> = by_expert.keys().copied().collect();
        let staged = experts.begin_fetch(gpu, self.layer, &distinct)?;
        ensure!(
            staged.addrs.len() == distinct.len() && staged.ready.len() == distinct.len(),
            "expert source returned {} records for {} experts",
            staged.addrs.len(),
            distinct.len()
        );
        // Resident experts first so they can run before the rest arrive.
        let lists: Vec<Vec<usize>> = by_expert.into_values().collect();
        let mut order: Vec<usize> = (0..lists.len()).collect();
        order.sort_by_key(|&i| !staged.ready[i]);
        Ok(RoutedPlan {
            resident: staged.ready.iter().filter(|&&r| r).count(),
            recs: order.iter().map(|&i| staged.addrs[i]).collect(),
            lists: order.into_iter().map(|i| lists[i].clone()).collect(),
        })
    }

    /// Fused routed-expert path: per group (resident experts, then the ones that had
    /// to load) one gate/up and one down launch, then one deterministic slot-order
    /// combine, reading NVFP4 records in place. An assignment's output depends only
    /// on its own inputs and the kernel choice, which is made once per step, so the
    /// grouping never changes results.
    #[allow(clippy::too_many_arguments)]
    fn finish_routed(
        &self,
        gpu: &Gpu,
        d: &Dims,
        ws: &mut Workspace,
        x: &Buf,
        t: usize,
        plan: RoutedPlan,
        weights: &Buf,
        experts: &mut dyn ExpertSource,
    ) -> Result<Buf> {
        let (h, k) = (d.hidden, d.top_k);
        let a_total = t * k;
        let n_e = plan.lists.len();
        // meta = off[n_e + 1] ++ assign_tok[A] ++ slot_assign[A]
        let mut meta = vec![0i32; n_e + 1 + 2 * a_total];
        let mut a = 0usize;
        for (ei, slots) in plan.lists.iter().enumerate() {
            meta[ei] = a as i32;
            for &slot in slots {
                meta[n_e + 1 + a] = (slot / k) as i32;
                meta[n_e + 1 + a_total + slot] = a as i32;
                a += 1;
            }
        }
        meta[n_e] = a as i32;
        let tiled = plan.lists.iter().map(Vec::len).max().unwrap_or(0) >= TILED_MIN_ASSIGNMENTS;

        let it = self.geo.inter;
        let mut tables = self.tables.lock().unwrap();
        gpu.write_into(&meta, &mut tables.meta)?;
        gpu.write_into(&plan.recs, &mut tables.recs)?;
        // Every element is written before it is read: h and y cover all assignments,
        // and g and u only feed the tiled path, which writes them first.
        let mut buf = Act {
            h: ws.take(gpu, "moe.h", a_total * it)?,
            y: ws.take(gpu, "moe.y", a_total * h)?,
            gu: if tiled {
                Some((
                    ws.take(gpu, "moe.g", a_total * it)?,
                    ws.take(gpu, "moe.u", a_total * it)?,
                ))
            } else {
                None
            },
        };
        for (lo, hi) in [(0, plan.resident), (plan.resident, n_e)] {
            if lo == plan.resident {
                experts.finish_fetch(gpu)?;
            }
            if lo == hi {
                continue;
            }
            let max_n = plan.lists[lo..hi].iter().map(Vec::len).max().unwrap_or(0);
            let assigns = (meta[lo] as usize, meta[hi] as usize);
            self.run_group(
                gpu,
                &tables,
                &mut buf,
                x,
                (lo, hi),
                n_e,
                a_total,
                assigns,
                max_n,
            )?;
        }
        let slot_assign = tables.meta.slice(n_e + 1 + a_total..n_e + 1 + 2 * a_total);
        let mut routed = gpu.zeros(t * h)?;
        gpu.moe_combine_slots(&buf.y, &slot_assign, weights, &mut routed, t, h, k)?;
        ws.give("moe.h", buf.h);
        ws.give("moe.y", buf.y);
        if let Some((g, u)) = buf.gu {
            ws.give("moe.g", g);
            ws.give("moe.u", u);
        }
        Ok(routed)
    }

    /// Runs the plan's experts `[lo, hi)`, whose assignments are `[a0, a1)`, writing
    /// their outputs into `buf.y`. The tiled kernels are used exactly when `buf.gu`
    /// holds buffers.
    #[allow(clippy::too_many_arguments)]
    fn run_group(
        &self,
        gpu: &Gpu,
        tables: &Tables,
        buf: &mut Act,
        x: &Buf,
        (lo, hi): (usize, usize),
        n_e: usize,
        a_total: usize,
        (a0, a1): (usize, usize),
        max_n: usize,
    ) -> Result<()> {
        let geo = &self.geo;
        let recs = tables.recs.slice(lo..hi);
        let off = tables.meta.slice(lo..hi + 1);
        let assign = tables.meta.slice(n_e + 1..n_e + 1 + a_total);
        let n = hi - lo;
        match &mut buf.gu {
            Some((g, u)) => {
                let (hd, it) = (geo.hidden, geo.inter);
                let gate = (geo.gate_weight, geo.gate_scale, geo.scale2[0]);
                let up = (geo.up_weight, geo.up_scale, geo.scale2[1]);
                let down = (geo.down_weight, geo.down_scale, geo.scale2[2]);
                let rows = Some(&assign);
                gpu.moe_tiled(&recs, &off, rows, n, max_n, x, g, it, hd, gate)?;
                gpu.moe_tiled(&recs, &off, rows, n, max_n, x, u, it, hd, up)?;
                gpu.moe_swiglu_range(g, u, &mut buf.h, a0 * it, a1 * it)?;
                gpu.moe_tiled(
                    &recs, &off, None, n, max_n, &buf.h, &mut buf.y, hd, it, down,
                )?;
            }
            None => {
                gpu.moe_gate_up(&recs, &off, &assign, n, x, &mut buf.h, geo)?;
                gpu.moe_down(&recs, &off, n, &buf.h, &mut buf.y, geo)?;
            }
        }
        Ok(())
    }
}

/// Activation buffers of one fused step, borrowed from the sequence workspace.
struct Act {
    /// SwiGLU activations `[A, inter]`.
    h: Buf,
    /// Per-assignment down outputs `[A, hidden]`.
    y: Buf,
    /// Gate and up outputs `[A, inter]` (tiled path only).
    gu: Option<(Buf, Buf)>,
}

/// A step's routed experts in launch order (resident first) with their
/// assignment slots and record addresses.
struct RoutedPlan {
    lists: Vec<Vec<usize>>,
    recs: Vec<u64>,
    /// The first `resident` experts were usable before `finish_fetch`.
    resident: usize,
}
