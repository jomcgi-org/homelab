//! Sparse MoE: router, shared expert and routed experts read in place from their
//! records: NVFP4 (W4A16 in fp32) for the decoder layers, bf16 for the MTP layer.

use std::sync::{Arc, Mutex};

use anyhow::{Context, Result, bail, ensure};
use oominf_core::{
    Backend, Bf16Record, ExpertPrecision, ExpertSource, Nvfp4Record, Probe, View, Weight,
    Workspace, tap,
};
use oominf_cpu::{HostExperts, Job, Pending};
use oominf_format::Model;

use crate::Dims;
use crate::model::PREFILL_FETCH_TOKENS;
use crate::util::{exact_weight, exact_weight_concat, weight, weight_concat};

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

/// Most tokens in a step that still routes this layer and predicts the next one
/// with one stacked router GEMV (decode and draft verification).
const PAIR_MAX_TOKENS: usize = 4;

/// Steps where some expert has at least this many assignments use the tiled
/// (shared-memory) kernels; smaller steps use the warp-per-row kernels.
const TILED_MIN_ASSIGNMENTS: usize = 32;

/// How a group's expert records are laid out.
#[derive(Debug, Clone, Copy)]
enum Geometry {
    Nvfp4(Nvfp4Record),
    Bf16(Bf16Record),
}

impl Geometry {
    fn inter(&self) -> usize {
        match self {
            Geometry::Nvfp4(g) => g.inter,
            Geometry::Bf16(g) => g.inter,
        }
    }

    fn host(&self) -> oominf_cpu::Geometry {
        match *self {
            Geometry::Nvfp4(g) => oominf_cpu::Geometry::Nvfp4(g),
            Geometry::Bf16(g) => oominf_cpu::Geometry::Bf16(g),
        }
    }
}

pub struct Moe<B: Backend> {
    /// The expert group (and tier key) this MoE routes into.
    layer: u32,
    geo: Geometry,
    precision: ExpertPrecision,
    tables: Mutex<Tables<B>>,
    router: Weight<B>,
    /// This layer's router stacked over the next layer's (`[2 * experts, hidden]`):
    /// one decode GEMV routes this layer and predicts the next (absent for the last
    /// layer).
    router_pair: Option<Weight<B>>,
    /// The shared expert's gate over its up projection (`[2 * shared_inter,
    /// hidden]`): one GEMM for both.
    shared_gate_up: Weight<B>,
    shared_down: Weight<B>,
    shared_gate_logit: Weight<B>,
    /// Computes host-resident experts of decode-sized steps on the CPU (absent:
    /// every routed record is copied to the device).
    host: Option<Arc<HostExperts>>,
    /// Bytes of one expert record.
    stride: usize,
}

impl<B: Backend> Moe<B> {
    /// Lets decode-sized steps compute host-resident experts on `pool`.
    pub fn set_host_experts(&mut self, pool: Arc<HostExperts>) {
        self.host = Some(pool);
    }

    /// A decoder layer's MoE.
    pub fn load(gpu: &B, model: &Model, d: &Dims, layer: u32) -> Result<Self> {
        let next = format!("model.language_model.layers.{}.mlp.gate.weight", layer + 1);
        let next = model.tensor(&next).map(|_| next);
        Self::load_group(
            gpu,
            model,
            d,
            layer,
            &format!("model.language_model.layers.{layer}.mlp."),
            next,
        )
    }

    /// The MoE whose dense weights are under `prefix` and whose routed experts are
    /// expert group `layer`; `next_router` names the next layer's router, which is
    /// stacked with this one to predict its experts.
    pub fn load_group(
        gpu: &B,
        model: &Model,
        d: &Dims,
        layer: u32,
        prefix: &str,
        next_router: Option<String>,
    ) -> Result<Self> {
        let p = prefix;
        let h = d.hidden as u64;
        let group = model
            .expert_group(layer)
            .with_context(|| format!("no experts for group {layer}"))?;
        ensure!(
            group.num_experts as usize == d.experts,
            "group {layer} has {} experts",
            group.num_experts
        );
        let part = |n: &str| {
            group
                .schema
                .part(n)
                .with_context(|| format!("expert record has no part {n}"))
        };
        let geo = match group.schema.layout.as_str() {
            "nvfp4-modelopt-g16" => {
                // (weight offset, scale offset, rows, columns) of one projection.
                let proj = |name: &str| -> Result<(usize, usize, usize, usize)> {
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
                Geometry::Nvfp4(Nvfp4Record {
                    hidden: gate.3,
                    inter: gate.2,
                    gate_weight: gate.0,
                    gate_scale: gate.1,
                    up_weight: up.0,
                    up_scale: up.1,
                    down_weight: down.0,
                    down_scale: down.1,
                    scale2: SCALE2_IDX,
                })
            }
            "bf16" => {
                let (g, u, dn) = (
                    part("gate.weight")?,
                    part("up.weight")?,
                    part("down.weight")?,
                );
                let (it, hd) = (g.shape[0] as usize, g.shape[1] as usize);
                ensure!(
                    it == d.moe_inter
                        && hd == d.hidden
                        && u.shape == g.shape
                        && dn.shape == [hd as u64, it as u64]
                        && [g.dtype.as_str(), u.dtype.as_str(), dn.dtype.as_str()] == ["BF16"; 3],
                    "bf16 expert shapes do not match config"
                );
                Geometry::Bf16(Bf16Record {
                    hidden: hd,
                    inter: it,
                    gate_weight: g.offset as usize,
                    up_weight: u.offset as usize,
                    down_weight: dn.offset as usize,
                })
            }
            other => bail!("unsupported expert layout {other}"),
        };
        let si = d.shared_inter as u64;
        let router_pair = match next_router {
            Some(next) => Some(exact_weight_concat(
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
            precision: d.expert_precision,
            // Sized for the largest step (a prefill fetch group) up front: grown
            // mid-prefill, 48 small long-lived buffers among the step's temporaries
            // pin pool blocks the allocator cannot trim (about 1 GB at 256k tokens).
            tables: Mutex::new(Tables {
                meta: gpu.upload_i32(&vec![
                    0;
                    (d.experts + 1 + 2 * PREFILL_FETCH_TOKENS * d.top_k)
                        .next_power_of_two()
                ])?,
                recs: gpu.zeros_u64(d.experts.next_power_of_two())?,
            }),
            router: exact_weight(
                gpu,
                model,
                &format!("{p}gate.weight"),
                &[d.experts as u64, h],
            )?,
            router_pair,
            shared_gate_up: weight_concat(
                gpu,
                model,
                &[
                    (format!("{p}shared_expert.gate_proj.weight"), vec![si, h]),
                    (format!("{p}shared_expert.up_proj.weight"), vec![si, h]),
                ],
                d,
            )?,
            shared_down: weight(
                gpu,
                model,
                &format!("{p}shared_expert.down_proj.weight"),
                &[h, si],
                d,
            )?,
            shared_gate_logit: exact_weight(
                gpu,
                model,
                &format!("{p}shared_expert_gate.weight"),
                &[1, h],
            )?,
            host: None,
            stride: group.schema.stride as usize,
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

        // Decode-sized steps with a source that uses hints: route this layer and
        // predict the next one with a single GEMV over the stacked routers; both go
        // through one top-k launch and one download. Per token the logits are
        // `[own (e) | next (e)]`, so the top-k results read as `[t, 2k]`: own first.
        // A probe sees the plain routing.
        let pair = match &self.router_pair {
            Some(p)
                if t <= PAIR_MAX_TOKENS
                    && experts.wants_prefetch()
                    && !probe.wants("router_logits")
                    && !probe.wants("topk_weights") =>
            {
                Some(p)
            }
            _ => None,
        };
        // With the pair, each token's logits row is two rows of `e`.
        let (rows, n_out) = if pair.is_some() {
            (2 * t, 2 * e)
        } else {
            (t, e)
        };
        let mut logits = gpu.zeros(rows * e)?;
        gpu.gemm_w(
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
        // The picks and (for experts computed on the host) the MoE input go down
        // together; the shared expert, which does not depend on the routing, is
        // queued before the host waits, so the device computes it while the host
        // plans the routed experts.
        let ids_pending = gpu.download_start_i32(&ids, rows * k)?;
        let host_ok = self.host.is_some() && t <= PAIR_MAX_TOKENS;
        let x_pending = if host_ok {
            Some(gpu.download_start_f32(x, t * h)?)
        } else {
            None
        };
        let (shared, gate_logit) = self.shared(gpu, d, x, t, scratch, probe)?;
        let mut ids_host = gpu.download_wait_i32(ids_pending)?;
        let predicted = pair.map(|_| {
            let rows: Vec<Vec<i32>> = ids_host.chunks(k).map(<[i32]>::to_vec).collect();
            ids_host = rows.iter().step_by(2).flatten().copied().collect();
            let mut v: Vec<u32> = rows
                .iter()
                .skip(1)
                .step_by(2)
                .flatten()
                .map(|&i| i as u32)
                .collect();
            v.sort_unstable();
            v.dedup();
            v
        });
        // The routing weights of this layer's rows (`[t, k]`).
        let weights = if pair.is_some() && t > 1 {
            let mut own = gpu.uninit(t * k)?;
            gpu.copy_cols(&weights, &mut own, t, 2 * k, 0, k)?;
            own
        } else {
            weights
        };
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
        // and the resident experts' compute; host-resident experts the source leaves
        // in host memory start on the CPU now.
        let plan = self.begin_routed(gpu, d, t, &ids_host, experts, host_ok)?;
        let host_work = self.start_host(gpu, d, x_pending, t, &plan)?;

        let mut routed =
            self.finish_routed(gpu, d, ws, x, t, plan, host_work, &weights, experts)?;
        tap(gpu, probe, "routed_out", &mut routed)?;
        if let Some(p) = predicted {
            experts.prefetch(gpu, self.layer + 1, &p)?;
        }
        let mut out = gpu.zeros(t * h)?;
        gpu.moe_combine(&routed, &shared, &gate_logit, &mut out, t, h)?;
        tap(gpu, probe, "moe_out", &mut out)?;
        Ok(out)
    }

    /// Queues routing for `t` tokens of `x` (`[t, hidden]`): this layer's router
    /// (with `with_next` and a next layer, the stacked routers, so the next layer's
    /// prediction comes in the same download), top-k, and an asynchronous download
    /// of the picks into `host`. [`Moe::route_finish`] collects it, so the host can
    /// queue more work before waiting.
    #[allow(clippy::too_many_arguments)]
    pub fn route_start(
        &self,
        gpu: &B,
        d: &Dims,
        x: &B::F32,
        t: usize,
        scratch: &mut B::Bf16,
        with_next: bool,
        host: &mut HostIds<'_, B>,
    ) -> Result<PendingRoute<B>> {
        let (h, e, k) = (d.hidden, d.experts, d.top_k);
        let pair = self.router_pair.as_ref().filter(|_| with_next);
        // With the pair, per token the logits are `[own (e) | next (e)]` and top-k
        // rows alternate own, next.
        let rows = if pair.is_some() { 2 * t } else { t };
        ensure!(
            rows * k <= host.buf.len(),
            "routing download larger than its buffer"
        );
        let mut logits = gpu.zeros(rows * e)?;
        gpu.gemm_w(
            x,
            pair.unwrap_or(&self.router),
            &mut logits,
            scratch,
            t,
            rows / t * e,
            h,
        )?;
        let mut ids = gpu.upload_i32(&vec![0i32; rows * k])?;
        let mut weights = gpu.zeros(rows * k)?;
        gpu.router_topk(&logits, &mut ids, &mut weights, rows, e, k)?;
        let weights = if pair.is_some() {
            let mut own = gpu.uninit(t * k)?;
            gpu.copy_cols(&weights, &mut own, t, 2 * k, 0, k)?;
            own
        } else {
            weights
        };
        // SAFETY: `host` is pinned, holds `rows * k` elements, and is read only after
        // `route_finish` waits for `done`; `ids` lives in the pending route until then.
        unsafe { gpu.download_async(&ids, host.buf.as_mut_ptr(), rows * k)? };
        Ok(PendingRoute {
            ids,
            weights,
            t,
            paired: pair.is_some(),
            done: gpu.record_compute()?,
        })
    }

    /// Waits for a [`Moe::route_start`] download in `host`: the routing, and the
    /// next layer's predicted experts (distinct, sorted; empty without the pair).
    pub fn route_finish(
        &self,
        gpu: &B,
        d: &Dims,
        pending: PendingRoute<B>,
        host: &HostIds<'_, B>,
    ) -> Result<(Routing<B>, Vec<u32>)> {
        let k = d.top_k;
        gpu.event_wait(&pending.done)?;
        drop(pending.ids);
        if !pending.paired {
            let ids = host.buf[..pending.t * k].to_vec();
            return Ok((
                Routing {
                    ids,
                    weights: pending.weights,
                },
                Vec::new(),
            ));
        }
        let rows = &host.buf[..2 * pending.t * k];
        let ids = rows.chunks(k).step_by(2).flatten().copied().collect();
        let mut next: Vec<u32> = rows
            .chunks(k)
            .skip(1)
            .step_by(2)
            .flatten()
            .map(|&i| i as u32)
            .collect();
        next.sort_unstable();
        next.dedup();
        Ok((
            Routing {
                ids,
                weights: pending.weights,
            },
            next,
        ))
    }

    /// The MoE output for `t` tokens of `x` routed by `routing` (from
    /// [`Moe::route`] on the same `x`), with records from `experts`.
    #[allow(clippy::too_many_arguments)]
    pub fn apply(
        &self,
        gpu: &B,
        d: &Dims,
        ws: &mut Workspace<B>,
        x: &B::F32,
        t: usize,
        routing: Routing<B>,
        experts: &mut dyn ExpertSource<B>,
        scratch: &mut B::Bf16,
    ) -> Result<B::F32> {
        let h = d.hidden;
        // The shared expert first: the device runs it while the host plans the routed
        // experts.
        let (shared, gate_logit) = self.shared(gpu, d, x, t, scratch, &mut oominf_core::NoProbe)?;
        let plan = self.begin_routed(gpu, d, t, &routing.ids, experts, false)?;
        let routed = self.finish_routed(gpu, d, ws, x, t, plan, None, &routing.weights, experts)?;
        let mut out = gpu.zeros(t * h)?;
        gpu.moe_combine(&routed, &shared, &gate_logit, &mut out, t, h)?;
        Ok(out)
    }

    /// The shared expert's output and its gate logit for `t` tokens of `x`.
    fn shared(
        &self,
        gpu: &B,
        d: &Dims,
        x: &B::F32,
        t: usize,
        scratch: &mut B::Bf16,
        probe: &mut dyn Probe,
    ) -> Result<(B::F32, B::F32)> {
        let (h, si) = (d.hidden, d.shared_inter);
        let mut gu = gpu.uninit(t * 2 * si)?;
        gpu.gemm_w(x, &self.shared_gate_up, &mut gu, scratch, t, 2 * si, h)?;
        let mut sact = gpu.uninit(t * si)?;
        gpu.silu_mul_rows(&gu, &mut sact, t, si)?;
        let mut shared = gpu.zeros(t * h)?;
        gpu.gemm_w(&sact, &self.shared_down, &mut shared, scratch, t, h, si)?;
        tap(gpu, probe, "shared_out", &mut shared)?;
        let mut gate_logit = gpu.zeros(t)?;
        gpu.gemm_w(
            x,
            &self.shared_gate_logit,
            &mut gate_logit,
            scratch,
            t,
            1,
            h,
        )?;
        tap(gpu, probe, "shared_gate_logit", &mut gate_logit)?;
        Ok((shared, gate_logit))
    }

    /// Groups the step's assignments by expert (resident experts first, then those
    /// computed on the host, then those still loading) and starts fetching their
    /// records.
    fn begin_routed(
        &self,
        gpu: &B,
        d: &Dims,
        t: usize,
        ids_host: &[i32],
        experts: &mut dyn ExpertSource<B>,
        host_ok: bool,
    ) -> Result<RoutedPlan> {
        let (e, k) = (d.experts, d.top_k);
        // Group assignment slots (t * k + s) by expert, in expert order.
        let mut by_expert: Vec<Vec<usize>> = vec![Vec::new(); e];
        for (slot, &ex) in ids_host.iter().enumerate().take(t * k) {
            if ex < 0 || ex as usize >= e {
                bail!("router picked expert {ex}");
            }
            by_expert[ex as usize].push(slot);
        }
        let distinct: Vec<u32> = (0..e as u32)
            .filter(|&ex| !by_expert[ex as usize].is_empty())
            .collect();
        let staged = experts.begin_fetch(gpu, self.layer, &distinct, host_ok)?;
        ensure!(
            staged.addrs.len() == distinct.len()
                && staged.ready.len() == distinct.len()
                && staged.host.len() == distinct.len(),
            "expert source returned {} records for {} experts",
            staged.addrs.len(),
            distinct.len()
        );
        // Resident experts first so they can run before the rest arrive, then the
        // host-computed ones, then those still loading.
        let mut lists: Vec<Vec<usize>> = by_expert.into_iter().filter(|l| !l.is_empty()).collect();
        let rank = |i: usize| match (staged.ready[i], staged.host[i]) {
            (true, _) => 0,
            (false, Some(_)) => 1,
            (false, None) => 2,
        };
        let mut order: Vec<usize> = (0..lists.len()).collect();
        order.sort_by_key(|&i| rank(i));
        Ok(RoutedPlan {
            resident: order.iter().filter(|&&i| rank(i) == 0).count(),
            hosted: order.iter().filter(|&&i| rank(i) == 1).count(),
            recs: order.iter().map(|&i| staged.addrs[i]).collect(),
            host: order.iter().map(|&i| staged.host[i]).collect(),
            lists: order
                .into_iter()
                .map(|i| std::mem::take(&mut lists[i]))
                .collect(),
        })
    }

    /// Starts the plan's host-computed experts on the CPU (their records stay in
    /// host memory until the source's next fetch, which follows this step).
    fn start_host(
        &self,
        gpu: &B,
        d: &Dims,
        x: Option<B::Download>,
        t: usize,
        plan: &RoutedPlan,
    ) -> Result<Option<Pending>> {
        let (Some(pool), true) = (&self.host, plan.hosted > 0) else {
            return Ok(None);
        };
        let x = x.context("host-computed experts without their input")?;
        let mut xs = gpu.download_wait_f32(x)?;
        xs.truncate(t * d.hidden);
        let k = d.top_k;
        let range = plan.resident..plan.resident + plan.hosted;
        let jobs = range
            .map(|i| {
                Ok(Job {
                    record: plan.host[i].context("host-computed expert without a record")?,
                    len: self.stride,
                    tokens: plan.lists[i].iter().map(|&slot| slot / k).collect(),
                })
            })
            .collect::<Result<Vec<_>>>()?;
        // SAFETY: the source keeps these host records unchanged until its next fetch
        // or prefetch, and this step waits for the work (or drops `Pending`, which
        // waits) before either.
        Ok(Some(unsafe { pool.submit(self.geo.host(), xs, jobs) }))
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
        mut host_work: Option<Pending>,
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
        ensure!(
            !tiled || matches!(self.geo, Geometry::Nvfp4(_)),
            "bf16 expert records only run decode-sized steps"
        );

        let it = self.geo.inter();
        let mut tables = self.tables.lock().unwrap();
        gpu.write_i32(&meta, &mut tables.meta)?;
        gpu.write_u64(&plan.recs, &mut tables.recs)?;
        // Every element is written before it is read: h and y cover all assignments,
        // and g and u only feed the tiled path, which writes them first.
        // Tiled (prefill) steps vary in size from group to group: reuse the largest
        // buffers. Decode steps take exact sizes, which gives the large ones back.
        let mut take = |name, n| {
            if tiled {
                ws.take_at_least(gpu, name, n)
            } else {
                ws.take(gpu, name, n)
            }
        };
        let mut buf = Act {
            h: take("moe.h", a_total * it)?,
            y: take("moe.y", a_total * h)?,
            gu: if tiled {
                Some((take("moe.g", a_total * it)?, take("moe.u", a_total * it)?))
            } else {
                None
            },
        };
        let loading = plan.resident + plan.hosted;
        for (lo, hi) in [(0, plan.resident), (loading, n_e)] {
            if lo == loading {
                experts.finish_fetch(gpu)?;
                if let Some(work) = host_work.take() {
                    // The host-computed experts' outputs land in their assignment rows.
                    let y = work.wait()?;
                    let a0 = meta[plan.resident] as usize;
                    gpu.write_f32_at(&y, &mut buf.y, a0 * h)?;
                }
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
        let recs = View::new(&tables.recs, lo, hi - lo);
        let off = View::new(&tables.meta, lo, hi - lo + 1);
        let assign = View::new(&tables.meta, n_e + 1, a_total);
        let n = hi - lo;
        match (&self.geo, &mut buf.gu) {
            (Geometry::Bf16(geo), None) => {
                gpu.moe_gate_up_bf16(&recs, &off, &assign, n, x, &mut buf.h, geo)?;
                gpu.moe_down_bf16(&recs, &off, n, &buf.h, &mut buf.y, geo)?;
            }
            (Geometry::Bf16(_), Some(_)) => bail!("bf16 expert records have no tiled path"),
            (Geometry::Nvfp4(geo), Some((g, u))) => {
                let (hd, it) = (geo.hidden, geo.inter);
                let gate = (geo.gate_weight, geo.gate_scale, geo.scale2[0]);
                let up = (geo.up_weight, geo.up_scale, geo.scale2[1]);
                let down = (geo.down_weight, geo.down_scale, geo.scale2[2]);
                let (rows, p) = (Some(&assign), self.precision);
                gpu.moe_tiled(&recs, &off, rows, n, max_n, x, g, it, hd, gate, p)?;
                gpu.moe_tiled(&recs, &off, rows, n, max_n, x, u, it, hd, up, p)?;
                gpu.moe_swiglu_range(g, u, &mut buf.h, a0 * it, a1 * it)?;
                gpu.moe_tiled(
                    &recs, &off, None, n, max_n, &buf.h, &mut buf.y, hd, it, down, p,
                )?;
            }
            (Geometry::Nvfp4(geo), None) => {
                gpu.moe_gate_up(&recs, &off, &assign, n, x, &mut buf.h, geo)?;
                gpu.moe_down(&recs, &off, n, &buf.h, &mut buf.y, geo)?;
            }
        }
        Ok(())
    }
}

/// A step's routing: each token's experts (`[t, k]`, host) and weights (`[t, k]`).
pub struct Routing<B: Backend> {
    ids: Vec<i32>,
    weights: B::F32,
}

/// Routing queued by [`Moe::route_start`], not yet collected.
pub struct PendingRoute<B: Backend> {
    /// The device picks, kept until their download lands.
    ids: B::I32,
    weights: B::F32,
    t: usize,
    paired: bool,
    done: B::Event,
}

/// Pinned host memory that routing picks download into asynchronously. Dropping
/// it waits for the device, so no copy can still be writing into it.
pub struct HostIds<'a, B: Backend> {
    gpu: &'a B,
    buf: Box<[i32]>,
}

impl<'a, B: Backend> HostIds<'a, B> {
    pub fn new(gpu: &'a B, n: usize) -> Result<Self> {
        let mut buf = vec![0i32; n.max(1)].into_boxed_slice();
        // SAFETY: the buffer outlives its registration (unpinned in Drop).
        unsafe { gpu.pin_host(buf.as_mut_ptr().cast(), buf.len() * 4)? };
        Ok(Self { gpu, buf })
    }
}

impl<B: Backend> Drop for HostIds<'_, B> {
    fn drop(&mut self) {
        let _ = self.gpu.sync();
        // SAFETY: pinned in `new`; the device has finished every copy into it.
        unsafe { self.gpu.unpin_host(self.buf.as_mut_ptr().cast()) };
    }
}

impl<B: Backend> Routing<B> {
    /// The expert of every assignment (repeats included).
    pub fn experts(&self) -> impl Iterator<Item = u32> + '_ {
        self.ids.iter().map(|&i| i as u32)
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

/// A step's routed experts in launch order (resident, host-computed, loading) with
/// their assignment slots and record addresses.
struct RoutedPlan {
    lists: Vec<Vec<usize>>,
    /// Device record addresses (unused for host-computed experts).
    recs: Vec<u64>,
    /// Host record addresses of host-computed experts.
    host: Vec<Option<usize>>,
    /// The first `resident` experts were usable before `finish_fetch`.
    resident: usize,
    /// The next `hosted` experts are computed on the host.
    hosted: usize,
}
