//! Decode operations for hybrid recurrent and grouped-query attention.
use super::{Dev, Gpu};
use anyhow::{Result, ensure};

impl Gpu {
    pub fn shared_gate(&self, x: &mut Dev<f32>, gate: &Dev<f32>, n: usize) -> Result<()> {
        ensure!(x.len >= n && gate.len >= 1, "shared gate buffer invalid");
        self.launch(
            "shared_gate",
            &[x.buffer(), gate.buffer()],
            &[n as u32],
            n,
            128,
        )
    }
    pub fn embedding(
        &self,
        weights: &Dev<u16>,
        token: usize,
        hidden: usize,
        out: &mut Dev<f32>,
    ) -> Result<()> {
        ensure!(
            hidden > 0 && token < weights.len / hidden && out.len >= hidden,
            "embedding range invalid"
        );
        self.launch(
            "embedding",
            &[weights.buffer(), out.buffer()],
            &[token as u32, hidden as u32],
            hidden,
            128,
        )
    }

    pub fn causal_conv(
        &self,
        x: &Dev<f32>,
        weight: &Dev<u16>,
        history: &mut Dev<f32>,
        out: &mut Dev<f32>,
        channels: usize,
        kernel: usize,
    ) -> Result<()> {
        ensure!(
            kernel > 0
                && x.len >= channels
                && out.len >= channels
                && weight.len >= channels * kernel
                && history.len >= channels * (kernel - 1),
            "convolution geometry invalid"
        );
        self.launch(
            "causal_conv",
            &[x.buffer(), weight.buffer(), history.buffer(), out.buffer()],
            &[channels as u32, kernel as u32],
            channels,
            128,
        )
    }

    /// q/k are L2-normalized in the packed qkv input. State is [value_head, value_dim, key_dim].
    #[allow(clippy::too_many_arguments)]
    pub fn delta_step(
        &self,
        qkv: &Dev<f32>,
        a: &Dev<f32>,
        b: &Dev<f32>,
        log_a: &Dev<u16>,
        bias: &Dev<u16>,
        state: &mut Dev<f32>,
        out: &mut Dev<f32>,
        key_heads: usize,
        value_heads: usize,
        dk: usize,
        dv: usize,
    ) -> Result<()> {
        ensure!(
            key_heads > 0
                && value_heads.is_multiple_of(key_heads)
                && dk > 0
                && dv > 0
                && qkv.len >= 2 * key_heads * dk + value_heads * dv
                && a.len >= value_heads
                && b.len >= value_heads
                && log_a.len >= value_heads
                && bias.len >= value_heads
                && state.len >= value_heads * dv * dk
                && out.len >= value_heads * dv,
            "delta geometry invalid"
        );
        self.launch(
            "delta_step",
            &[
                qkv.buffer(),
                a.buffer(),
                b.buffer(),
                log_a.buffer(),
                bias.buffer(),
                state.buffer(),
                out.buffer(),
            ],
            &[key_heads as u32, value_heads as u32, dk as u32, dv as u32],
            value_heads * dv * 32,
            128,
        )
    }

    pub fn rope_half(
        &self,
        x: &mut Dev<f32>,
        heads: usize,
        d: usize,
        rotary: usize,
        position: usize,
        theta: f32,
    ) -> Result<()> {
        ensure!(
            rotary > 0
                && rotary.is_multiple_of(2)
                && rotary <= d
                && x.len >= heads * d
                && theta > 0.,
            "rotary geometry invalid"
        );
        self.launch(
            "rope_half",
            &[x.buffer()],
            &[d as u32, rotary as u32, position as u32, theta.to_bits()],
            heads * rotary / 2,
            128,
        )
    }

    pub fn kv_append(
        &self,
        x: &Dev<f32>,
        cache: &mut Dev<f32>,
        row: usize,
        width: usize,
    ) -> Result<()> {
        ensure!(
            width > 0 && x.len >= width && row < cache.len / width,
            "KV append out of bounds"
        );
        self.launch(
            "kv_append",
            &[x.buffer(), cache.buffer()],
            &[row as u32, width as u32],
            width,
            128,
        )
    }

    #[allow(clippy::too_many_arguments)]
    pub fn gqa_step(
        &self,
        q: &Dev<f32>,
        k: &Dev<f32>,
        v: &Dev<f32>,
        out: &mut Dev<f32>,
        scores: &mut Dev<f32>,
        heads: usize,
        kv_heads: usize,
        d: usize,
        len: usize,
    ) -> Result<()> {
        ensure!(
            kv_heads > 0
                && heads.is_multiple_of(kv_heads)
                && d > 0
                && len > 0
                && q.len >= heads * d
                && out.len >= heads * d
                && k.len >= len * kv_heads * d
                && v.len >= len * kv_heads * d
                && scores.len >= heads * len,
            "GQA geometry invalid"
        );
        self.launch(
            "gqa_step",
            &[
                q.buffer(),
                k.buffer(),
                v.buffer(),
                out.buffer(),
                scores.buffer(),
            ],
            &[heads as u32, kv_heads as u32, d as u32, len as u32],
            heads * 32,
            32,
        )
    }

    pub fn scaled_add(&self, x: &Dev<f32>, y: &mut Dev<f32>, scale: f32, n: usize) -> Result<()> {
        ensure!(x.len >= n && y.len >= n, "scaled add out of bounds");
        self.launch(
            "scaled_add",
            &[x.buffer(), y.buffer()],
            &[n as u32, scale.to_bits()],
            n,
            128,
        )
    }

    /// Matrix projection from a tier-managed record. The command retains its Metal allocation.
    #[allow(clippy::too_many_arguments)]
    pub fn gemm_record(
        &self,
        x: &Dev<f32>,
        address: u64,
        weight: usize,
        scale: usize,
        scalar: usize,
        y: &mut Dev<f32>,
        n: usize,
        k: usize,
    ) -> Result<()> {
        ensure!(
            k > 0 && k.is_multiple_of(16) && x.len >= k && y.len >= n,
            "expert projection geometry invalid"
        );
        let (w, wo) = self.address(address + weight as u64, n * k / 2)?;
        let (s, so) = self.address(address + scale as u64, n * k / 16)?;
        let (global, go) = self.address(address + scalar as u64 * 4, 4)?;
        // SAFETY: tier fetch completed its copies, and record scalars are immutable
        // until the next fetch, which occurs after these commands finish.
        let scale2 = unsafe {
            std::ptr::read_unaligned(global.buffer.contents().cast::<u8>().add(go).cast::<f32>())
        };
        self.launch_offsets(
            "gemm_nvfp4",
            &[
                (x.buffer(), 0),
                (&w.buffer, wo as u64),
                (&s.buffer, so as u64),
                (y.buffer(), 0),
            ],
            &[1, n as u32, k as u32, scale2.to_bits()],
            n * 32,
            128,
        )
    }
}
