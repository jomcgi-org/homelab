//! Routing and fused routed-expert kernels on NVFP4 and bf16 records: `kernels/moe.cu`.

use anyhow::Result;
use cudarc::driver::{LaunchConfig, PushKernelArg};
use oominf_core::{Bf16Record, Experts, Nvfp4Record, View};

use crate::{Buf, Dev, Gpu, cview, grid};

impl Experts for Gpu {
    fn router_topk(
        &self,
        logits: &Buf,
        ids: &mut Dev<i32>,
        weights: &mut Buf,
        t: usize,
        e: usize,
        k: usize,
    ) -> Result<()> {
        self.check(k <= 32 && e <= 8192, "router top-k > 32 or experts > 8192")?;
        let f = self.func("router_topk")?;
        let (e32, k32) = (e as i32, k as i32);
        let cfg = LaunchConfig {
            grid_dim: (t as u32, 1, 1),
            block_dim: (256, 1, 1),
            shared_mem_bytes: (e * 4) as u32,
        };
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(logits)
                .arg(ids)
                .arg(weights)
                .arg(&e32)
                .arg(&k32)
                .launch(cfg)?
        };
        Ok(())
    }

    /// `h[a] = silu(x_t . Wg) * (x_t . Wu)` for every assignment `a = (t, e)`;
    /// assignments of expert `e` are `[off[e], off[e + 1])` of `assign_tok`.
    #[allow(clippy::too_many_arguments)]
    fn moe_gate_up(
        &self,
        recs: &View<Dev<u64>>,
        off: &View<Dev<i32>>,
        assign_tok: &View<Dev<i32>>,
        n_experts: usize,
        x: &Buf,
        h: &mut Buf,
        geo: &Nvfp4Record,
    ) -> Result<()> {
        let recs = cview(recs);
        let off = cview(off);
        let assign_tok = cview(assign_tok);
        self.check(
            geo.hidden.is_multiple_of(16) && geo.inter.is_multiple_of(16),
            "moe_gate_up: widths must be multiples of 16",
        )?;
        let f = self.func("moe_gate_up")?;
        let cfg = LaunchConfig {
            grid_dim: (geo.inter.div_ceil(8) as u32, n_experts as u32, 1),
            block_dim: (256, 1, 1),
            shared_mem_bytes: 0,
        };
        let (hh, ii) = (geo.hidden as i32, geo.inter as i32);
        let (gw, gs, uw, us) = (
            geo.gate_weight as i64,
            geo.gate_scale as i64,
            geo.up_weight as i64,
            geo.up_scale as i64,
        );
        let (g2, u2) = (geo.scale2[0] as i32, geo.scale2[1] as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(&recs)
                .arg(&off)
                .arg(&assign_tok)
                .arg(x)
                .arg(h)
                .arg(&hh)
                .arg(&ii)
                .arg(&gw)
                .arg(&gs)
                .arg(&uw)
                .arg(&us)
                .arg(&g2)
                .arg(&u2)
                .launch(cfg)?
        };
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn moe_gate_up_bf16(
        &self,
        recs: &View<Dev<u64>>,
        off: &View<Dev<i32>>,
        assign_tok: &View<Dev<i32>>,
        n_experts: usize,
        x: &Buf,
        h: &mut Buf,
        geo: &Bf16Record,
    ) -> Result<()> {
        let recs = cview(recs);
        let off = cview(off);
        let assign_tok = cview(assign_tok);
        self.check(
            geo.hidden.is_multiple_of(8) && geo.inter.is_multiple_of(8),
            "moe_gate_up_bf16: widths must be multiples of 8",
        )?;
        let f = self.func("moe_gate_up_bf16")?;
        let cfg = LaunchConfig {
            grid_dim: (geo.inter.div_ceil(8) as u32, n_experts as u32, 1),
            block_dim: (256, 1, 1),
            shared_mem_bytes: 0,
        };
        let (hh, ii) = (geo.hidden as i32, geo.inter as i32);
        let (gw, uw) = (geo.gate_weight as i64, geo.up_weight as i64);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(&recs)
                .arg(&off)
                .arg(&assign_tok)
                .arg(x)
                .arg(h)
                .arg(&hh)
                .arg(&ii)
                .arg(&gw)
                .arg(&uw)
                .launch(cfg)?
        };
        Ok(())
    }

    fn moe_down_bf16(
        &self,
        recs: &View<Dev<u64>>,
        off: &View<Dev<i32>>,
        n_experts: usize,
        h: &Buf,
        y: &mut Buf,
        geo: &Bf16Record,
    ) -> Result<()> {
        let recs = cview(recs);
        let off = cview(off);
        let f = self.func("moe_down_bf16")?;
        let cfg = LaunchConfig {
            grid_dim: (geo.hidden.div_ceil(8) as u32, n_experts as u32, 1),
            block_dim: (256, 1, 1),
            shared_mem_bytes: 0,
        };
        let (hh, ii) = (geo.hidden as i32, geo.inter as i32);
        let dw = geo.down_weight as i64;
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(&recs)
                .arg(&off)
                .arg(h)
                .arg(y)
                .arg(&hh)
                .arg(&ii)
                .arg(&dw)
                .launch(cfg)?
        };
        Ok(())
    }

    /// `y[a] = h[a] . Wd^T` for every assignment.
    #[allow(clippy::too_many_arguments)]
    fn moe_down(
        &self,
        recs: &View<Dev<u64>>,
        off: &View<Dev<i32>>,
        n_experts: usize,
        h: &Buf,
        y: &mut Buf,
        geo: &Nvfp4Record,
    ) -> Result<()> {
        let recs = cview(recs);
        let off = cview(off);
        let f = self.func("moe_down")?;
        let cfg = LaunchConfig {
            grid_dim: (geo.hidden.div_ceil(8) as u32, n_experts as u32, 1),
            block_dim: (256, 1, 1),
            shared_mem_bytes: 0,
        };
        let (hh, ii) = (geo.hidden as i32, geo.inter as i32);
        let (dw, ds) = (geo.down_weight as i64, geo.down_scale as i64);
        let d2 = geo.scale2[2] as i32;
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(&recs)
                .arg(&off)
                .arg(h)
                .arg(y)
                .arg(&hh)
                .arg(&ii)
                .arg(&dw)
                .arg(&ds)
                .arg(&d2)
                .launch(cfg)?
        };
        Ok(())
    }

    /// Tiled grouped NVFP4 GEMM (prefill): `y[a] = x[row(a)] . W_e^T` for one
    /// projection (`w_off`, `s_off`, `s2_idx`), `row(a) = rows[a]` or `a`.
    #[allow(clippy::too_many_arguments)]
    fn moe_tiled(
        &self,
        recs: &View<Dev<u64>>,
        off: &View<Dev<i32>>,
        rows: Option<&View<Dev<i32>>>,
        n_experts: usize,
        max_per_expert: usize,
        x: &Buf,
        y: &mut Buf,
        n: usize,
        k: usize,
        proj: (usize, usize, usize),
    ) -> Result<()> {
        let (recs, off) = (cview(recs), cview(off));
        let rows = rows.map(cview);
        self.check(
            k.is_multiple_of(32),
            "moe_tiled: K must be a multiple of 32",
        )?;
        let f = self.func("moe_tiled")?;
        let cfg = LaunchConfig {
            grid_dim: (
                n.div_ceil(128) as u32,
                max_per_expert.div_ceil(32) as u32,
                n_experts as u32,
            ),
            block_dim: (128, 1, 1),
            shared_mem_bytes: 0,
        };
        let (n32, k32) = (n as i32, k as i32);
        let (wo, so, s2) = (proj.0 as i64, proj.1 as i64, proj.2 as i32);
        let null: u64 = 0;
        let mut b = self.stream.launch_builder(&f);
        b.arg(&recs).arg(&off);
        match &rows {
            Some(r) => b.arg(r),
            None => b.arg(&null),
        };
        unsafe {
            b.arg(x)
                .arg(y)
                .arg(&n32)
                .arg(&k32)
                .arg(&wo)
                .arg(&so)
                .arg(&s2)
                .launch(cfg)?
        };
        Ok(())
    }

    /// `h[i] = silu(g[i]) * u[i]` for `i` in `[start, end)`.
    fn moe_swiglu_range(
        &self,
        g: &Buf,
        u: &Buf,
        h: &mut Buf,
        start: usize,
        end: usize,
    ) -> Result<()> {
        self.check(
            start <= end && end <= g.len().min(u.len()).min(h.len()),
            "moe_swiglu_range bounds",
        )?;
        if start == end {
            return Ok(());
        }
        let f = self.func("moe_swiglu")?;
        let n32 = (end - start) as i32;
        let (gv, uv) = (g.slice(start..end), u.slice(start..end));
        let mut hv = h.slice_mut(start..end);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(&gv)
                .arg(&uv)
                .arg(&mut hv)
                .arg(&n32)
                .launch(grid(end - start, 256))?
        };
        Ok(())
    }

    /// `out[t] = sum_s w[t, s] * y[slot_assign[t, s]]` in slot order.
    #[allow(clippy::too_many_arguments)]
    fn moe_combine_slots(
        &self,
        y: &Buf,
        slot_assign: &View<Dev<i32>>,
        w: &Buf,
        out: &mut Buf,
        t: usize,
        h: usize,
        k: usize,
    ) -> Result<()> {
        let slot_assign = cview(slot_assign);
        let f = self.func("moe_combine_slots")?;
        let (t32, h32, k32) = (t as i32, h as i32, k as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(y)
                .arg(&slot_assign)
                .arg(w)
                .arg(out)
                .arg(&t32)
                .arg(&h32)
                .arg(&k32)
                .launch(grid(t * h, 256))?
        };
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn moe_combine(
        &self,
        routed: &Buf,
        shared: &Buf,
        gate_logit: &Buf,
        out: &mut Buf,
        t: usize,
        h: usize,
    ) -> Result<()> {
        let f = self.func("moe_combine")?;
        let (t32, h32) = (t as i32, h as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(routed)
                .arg(shared)
                .arg(gate_logit)
                .arg(out)
                .arg(&t32)
                .arg(&h32)
                .launch(grid(t * h, 256))?
        };
        Ok(())
    }
}
