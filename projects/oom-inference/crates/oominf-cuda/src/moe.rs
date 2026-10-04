//! Fused routed-expert kernels: `impl Gpu` launch wrappers for `kernels/moe.cu`.

use cudarc::driver::{CudaSlice, CudaView, LaunchConfig, PushKernelArg};

use crate::{Buf, Gpu, Result, grid};

/// Byte offsets of an NVFP4 expert record's parts and the index of each
/// projection's `weight_scale_2` in its leading f32 scalars.
#[derive(Debug, Clone, Copy)]
pub struct Nvfp4Record {
    /// Model width (gate/up input, down output).
    pub hidden: usize,
    /// Expert intermediate width (gate/up output, down input).
    pub inter: usize,
    pub gate_weight: usize,
    pub gate_scale: usize,
    pub up_weight: usize,
    pub up_scale: usize,
    pub down_weight: usize,
    pub down_scale: usize,
    /// `weight_scale_2` indices for gate, up, down.
    pub scale2: [usize; 3],
}

impl Gpu {
    /// Copies `host` into the front of `dst`, growing `dst` (reallocating) if needed.
    pub fn write_into<T: cudarc::driver::DeviceRepr + cudarc::driver::ValidAsZeroBits>(
        &self,
        host: &[T],
        dst: &mut CudaSlice<T>,
    ) -> Result<()> {
        if dst.len() < host.len() {
            *dst = self
                .stream
                .alloc_zeros::<T>(host.len().next_power_of_two())?;
        }
        self.stream
            .memcpy_htod(host, &mut dst.slice_mut(0..host.len()))?;
        Ok(())
    }

    /// Ensures `buf` holds at least `n` floats (contents unspecified after growth).
    pub fn ensure_len(&self, buf: &mut Buf, n: usize) -> Result<()> {
        if buf.len() < n {
            *buf = self.stream.alloc_zeros::<f32>(n.next_power_of_two())?;
        }
        Ok(())
    }

    /// `h[a] = silu(x_t . Wg) * (x_t . Wu)` for every assignment `a = (t, e)`;
    /// assignments of expert `e` are `[off[e], off[e + 1])` of `assign_tok`.
    #[allow(clippy::too_many_arguments)]
    pub fn moe_gate_up(
        &self,
        recs: &CudaSlice<u64>,
        off: &CudaView<i32>,
        assign_tok: &CudaView<i32>,
        n_experts: usize,
        x: &Buf,
        h: &mut Buf,
        geo: &Nvfp4Record,
    ) -> Result<()> {
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
                .arg(recs)
                .arg(off)
                .arg(assign_tok)
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

    /// `y[a] = h[a] . Wd^T` for every assignment.
    #[allow(clippy::too_many_arguments)]
    pub fn moe_down(
        &self,
        recs: &CudaSlice<u64>,
        off: &CudaView<i32>,
        n_experts: usize,
        h: &Buf,
        y: &mut Buf,
        geo: &Nvfp4Record,
    ) -> Result<()> {
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
                .arg(recs)
                .arg(off)
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

    /// `out[t] = sum_s w[t, s] * y[slot_assign[t, s]]` in slot order.
    #[allow(clippy::too_many_arguments)]
    pub fn moe_combine_slots(
        &self,
        y: &Buf,
        slot_assign: &CudaView<i32>,
        w: &Buf,
        out: &mut Buf,
        t: usize,
        h: usize,
        k: usize,
    ) -> Result<()> {
        let f = self.func("moe_combine_slots")?;
        let (t32, h32, k32) = (t as i32, h as i32, k as i32);
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(y)
                .arg(slot_assign)
                .arg(w)
                .arg(out)
                .arg(&t32)
                .arg(&h32)
                .arg(&k32)
                .launch(grid(t * h, 256))?
        };
        Ok(())
    }

    /// Tiled grouped NVFP4 GEMM (prefill): `y[a] = x[row(a)] . W_e^T` for one
    /// projection (`w_off`, `s_off`, `s2_idx`), `row(a) = rows[a]` or `a`.
    #[allow(clippy::too_many_arguments)]
    pub fn moe_tiled(
        &self,
        recs: &CudaSlice<u64>,
        off: &CudaView<i32>,
        rows: Option<&CudaView<i32>>,
        n_experts: usize,
        max_per_expert: usize,
        x: &Buf,
        y: &mut Buf,
        n: usize,
        k: usize,
        proj: (usize, usize, usize),
    ) -> Result<()> {
        self.check(
            k.is_multiple_of(32),
            "moe_tiled: K must be a multiple of 32",
        )?;
        let f = self.func("moe_tiled")?;
        let cfg = LaunchConfig {
            grid_dim: (
                n.div_ceil(64) as u32,
                max_per_expert.div_ceil(32) as u32,
                n_experts as u32,
            ),
            block_dim: (256, 1, 1),
            shared_mem_bytes: 0,
        };
        let (n32, k32) = (n as i32, k as i32);
        let (wo, so, s2) = (proj.0 as i64, proj.1 as i64, proj.2 as i32);
        let null: u64 = 0;
        let mut b = self.stream.launch_builder(&f);
        b.arg(recs).arg(off);
        match rows {
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

    /// `h = silu(g) * u`, elementwise over `n`.
    pub fn moe_swiglu(&self, g: &Buf, u: &Buf, h: &mut Buf, n: usize) -> Result<()> {
        let f = self.func("moe_swiglu")?;
        let n32 = n as i32;
        unsafe {
            self.stream
                .launch_builder(&f)
                .arg(g)
                .arg(u)
                .arg(h)
                .arg(&n32)
                .launch(grid(n, 256))?
        };
        Ok(())
    }
}
