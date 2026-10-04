//! Sparse MoE: router, shared expert and NVFP4 routed experts (W4A16 in fp32).

use std::collections::BTreeMap;
use std::sync::Mutex;

use anyhow::{Context, Result, bail, ensure};
use oominf_core::{Backend, ExpertSource, Nvfp4Record, Probe, View, Workspace, tap};
use oominf_format::Model;

use crate::Dims;
use crate::util::{bf16_concat, bf16_tensor};

/// Index of each projection's `weight_scale_2` in the record's leading scalars part
/// (gate.ws2, gate.in, up.ws2, up.in, down.ws2, down.in).
const SCALE2_IDX: [usize; 3] = [0, 2, 4];

/// Per-step routing tables. They are small (a few KiB), so each layer keeps its own;
/// the large activation buffers come from the sequence workspace, shared by every
/// layer.
struct Tables<B: Backend> {
    /// `off[n_e + 1] ++ assign_tok[A] ++ slot_assign[A]`.
    meta: B::I32,
    /// Record address of each expert of the step.
    recs: B::U64,
}

/// Steps where some expert has at least this many assignments use the tiled
/// (shared-memory) kernels; smaller steps use the warp-per-row kernels.
const TILED_MIN_ASSIGNMENTS: usize = 32;

pub struct Moe<B: Backend> {
    layer: u32,
    geo: Nvfp4Record,
    tables: Mutex<Tables<B>>,
    router: B::Bf16,
    /// This layer's router stacked over the next layer's (`[2 * experts, hidden]`):
    /// one decode GEMV routes this layer and predicts the next (absent for the last
    /// layer).
    router_pair: Option<B::Bf16>,
    shared_gate: B::Bf16,
    shared_up: B::Bf16,
    shared_down: B::Bf16,
    shared_gate_logit: B::Bf16,
}

impl<B: Backend> Moe<B> {
    pub fn load(gpu: &B, model: &Model, d: &Dims, layer: u32) -> Result<Self> {
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
        // (weight offset, scale offset, rows, columns) of one projection.
        let proj = |name: &str| -> Result<(usize, usize, usize, usize)> {
            let part = |n: &str| {
                group
                    .schema
                    .part(n)
                    .with_context(|| format!("expert record has no part {n}"))
            };
            let w = part(&format!("{name}.weight"))?;
            let s = part(&format!("{name}.weight_scale"))?;
            Ok((
                w.offset as usize,
                s.offset as usize,
                w.shape[0] as usize,
                2 * w.shape[1] as usize,
            ))
        };
        let (gate, up, down) = (proj("gate")?, proj("up")?, proj("down")?);
        ensure!(
            gate.2 == d.moe_inter && gate.3 == d.hidden && down.2 == d.hidden,
            "expert shapes do not match config"
        );
        ensure!(
            down.3 == gate.2,
            "expert down input does not match gate output"
        );
        let geo = Nvfp4Record {
            hidden: gate.3,
            inter: gate.2,
            gate_weight: gate.0,
            gate_scale: gate.1,
            up_weight: up.0,
            up_scale: up.1,
            down_weight: down.0,
            down_scale: down.1,
            scale2: SCALE2_IDX,
        };
        let si = d.shared_inter as u64;
        let w = |n: &str, s: &[u64]| bf16_tensor(gpu, model, &format!("{p}{n}"), s);
        let next = format!("model.language_model.layers.{}.mlp.gate.weight", layer + 1);
        let router_pair = match model.tensor(&next) {
            Some(_) => Some(bf16_concat(
                gpu,
                model,
                &[
                    (format!("{p}gate.weight"), vec![d.experts as u64, h]),
                    (next, vec![d.experts as u64, h]),
                ],
            )?),
            None => None,
        };
        Ok(Moe {
            layer,
            geo,
            tables: Mutex::new(Tables {
                meta: gpu.upload_i32(&[0; 64])?,
                recs: gpu.zeros_u64(64)?,
            }),
            router: w("gate.weight", &[d.experts as u64, h])?,
            router_pair,
            shared_gate: w("shared_expert.gate_proj.weight", &[si, h])?,
            shared_up: w("shared_expert.up_proj.weight", &[si, h])?,
            shared_down: w("shared_expert.down_proj.weight", &[h, si])?,
            shared_gate_logit: w("shared_expert_gate.weight", &[1, h])?,
        })
    }

    #[allow(clippy::too_many_arguments)]
    pub fn forward(
        &self,
        gpu: &B,
        d: &Dims,
        ws: &mut Workspace<B>,
        x: &B::F32,
        t: usize,
        experts: &mut dyn ExpertSource<B>,
        scratch: &mut B::Bf16,
        probe: &mut dyn Probe,
    ) -> Result<B::F32> {
        let (h, e, k) = (d.hidden, d.experts, d.top_k);

        // Decode with a source that uses hints: route this layer and predict the
        // next one with a single GEMV over the stacked routers; both rows go through
        // one top-k launch and one download. A probe sees the plain routing.
        let pair = match &self.router_pair {
            Some(p)
                if t == 1
                    && experts.wants_prefetch()
                    && !probe.wants("router_logits")
                    && !probe.wants("topk_weights") =>
            {
                Some(p)
            }
            _ => None,
        };
        // With the pair, the single token's logits row is two rows of `e`.
        let (rows, n_out) = if pair.is_some() { (2, 2 * e) } else { (t, e) };
        let mut logits = gpu.zeros(rows * e)?;
        gpu.gemm_bf16(
            x,
            pair.unwrap_or(&self.router),
            &mut logits,
            scratch,
            t,
            n_out,
            h,
        )?;
        tap(gpu, probe, "router_logits", &mut logits)?;
        let mut ids = gpu.upload_i32(&vec![0i32; rows * k])?;
        let mut weights = gpu.zeros(rows * k)?;
        gpu.router_topk(&logits, &mut ids, &mut weights, rows, e, k)?;
        tap(gpu, probe, "topk_weights", &mut weights)?;
        let mut ids_host = gpu.download_i32(&ids)?;
        let predicted = pair.map(|_| {
            let mut v: Vec<u32> = ids_host
                .split_off(k)
                .into_iter()
                .map(|i| i as u32)
                .collect();
            v.sort_unstable();
            v.dedup();
            v
        });
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
        let plan = self.begin_routed(gpu, d, t, &ids_host, experts)?;

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

        let mut routed = self.finish_routed(gpu, d, ws, x, t, plan, &weights, experts)?;
        tap(gpu, probe, "routed_out", &mut routed)?;
        if let Some(p) = predicted {
            experts.prefetch(gpu, self.layer + 1, &p)?;
        }
        let mut out = gpu.zeros(t * h)?;
        gpu.moe_combine(&routed, &shared, &gate_logit, &mut out, t, h)?;
        tap(gpu, probe, "moe_out", &mut out)?;
        Ok(out)
    }

    /// Groups the step's assignments by expert, resident experts first, and starts
    /// fetching their records.
    fn begin_routed(
        &self,
        gpu: &B,
        d: &Dims,
        t: usize,
        ids_host: &[i32],
        experts: &mut dyn ExpertSource<B>,
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

    /// Per group (resident experts, then the ones that had to load) one gate/up and
    /// one down launch, then one deterministic slot-order combine, reading NVFP4
    /// records in place. An assignment's output depends only on its own inputs and the
    /// kernel choice, which is made once per step, so the grouping never changes
    /// results.
    #[allow(clippy::too_many_arguments)]
    fn finish_routed(
        &self,
        gpu: &B,
        d: &Dims,
        ws: &mut Workspace<B>,
        x: &B::F32,
        t: usize,
        plan: RoutedPlan,
        weights: &B::F32,
        experts: &mut dyn ExpertSource<B>,
    ) -> Result<B::F32> {
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
        gpu.write_i32(&meta, &mut tables.meta)?;
        gpu.write_u64(&plan.recs, &mut tables.recs)?;
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
        let slot_assign = View::new(&tables.meta, n_e + 1 + a_total, a_total);
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
        gpu: &B,
        tables: &Tables<B>,
        buf: &mut Act<B>,
        x: &B::F32,
        (lo, hi): (usize, usize),
        n_e: usize,
        a_total: usize,
        (a0, a1): (usize, usize),
        max_n: usize,
    ) -> Result<()> {
        let geo = &self.geo;
        let recs = View::new(&tables.recs, lo, hi - lo);
        let off = View::new(&tables.meta, lo, hi - lo + 1);
        let assign = View::new(&tables.meta, n_e + 1, a_total);
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

/// Activation buffers of one step, borrowed from the sequence workspace.
struct Act<B: Backend> {
    /// SwiGLU activations `[A, inter]`.
    h: B::F32,
    /// Per-assignment down outputs `[A, hidden]`.
    y: B::F32,
    /// Gate and up outputs `[A, inter]` (tiled path only).
    gu: Option<(B::F32, B::F32)>,
}

/// A step's routed experts in launch order (resident first) with their
/// assignment slots and record addresses.
struct RoutedPlan {
    lists: Vec<Vec<usize>>,
    recs: Vec<u64>,
    /// The first `resident` experts were usable before `finish_fetch`.
    resident: usize,
}
