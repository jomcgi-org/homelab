//! Batched routed experts: selected records remain bound until compute completes.
use super::{Dev, Gpu};
use anyhow::{Result, ensure};
use oominf_core::Memory;

impl Gpu {
    /// Add selected NVFP4 SwiGLU experts in their supplied order. Six offsets
    /// identify gate weights/scales, up weights/scales and down weights/scales.
    /// Record headers hold the released gate/up/down global scales at 0/8/16.
    #[allow(clippy::too_many_arguments)]
    pub fn routed_swiglu(
        &self,
        x: &Dev<f32>,
        records: &[u64],
        weights: &[f32],
        offsets: [usize; 6],
        out: &mut Dev<f32>,
        hidden: usize,
        intermediate: usize,
    ) -> Result<()> {
        ensure!(
            !records.is_empty() && records.len() == weights.len(),
            "invalid routed expert selection"
        );
        ensure!(
            hidden > 0
                && intermediate > 0
                && hidden.is_multiple_of(16)
                && intermediate.is_multiple_of(16)
                && x.len >= hidden
                && out.len >= hidden,
            "invalid routed SwiGLU geometry"
        );
        let elements = hidden
            .checked_mul(intermediate)
            .ok_or_else(|| anyhow::anyhow!("expert size overflow"))?;
        let mut bytes = 24;
        for (i, offset) in offsets.iter().enumerate() {
            let size = if i.is_multiple_of(2) {
                elements / 2
            } else {
                elements / 16
            };
            bytes = bytes.max(
                offset
                    .checked_add(size)
                    .ok_or_else(|| anyhow::anyhow!("expert offset overflow"))?,
            );
        }
        for (addresses, weights) in records.chunks(8).zip(weights.chunks(8)) {
            let allocations = addresses
                .iter()
                .map(|&a| self.address(a, bytes))
                .collect::<Result<Vec<_>>>()?;
            let mut params = vec![
                addresses.len() as u32,
                u32::try_from(hidden)?,
                u32::try_from(intermediate)?,
            ];
            for offset in offsets {
                params.push(u32::try_from(offset)?);
            }
            for ((allocation, offset), weight) in allocations.iter().zip(weights) {
                // SAFETY: address() checked the entire immutable record; tier fetch
                // completed its copies. The next fetch follows compute completion.
                let header = unsafe { allocation.buffer.contents().cast::<u8>().add(*offset) };
                for offset in [0, 8, 16] {
                    let scalar =
                        unsafe { std::ptr::read_unaligned(header.add(offset).cast::<f32>()) };
                    params.push(scalar.to_bits());
                }
                params.push(weight.to_bits());
            }
            let products = self.uninit(addresses.len() * intermediate)?;
            let projections = self.uninit(addresses.len() * hidden)?;
            let mut bindings: Vec<_> = (0..8)
                .map(|i| {
                    let (allocation, offset) = &allocations[i.min(allocations.len() - 1)];
                    (allocation.buffer.as_ref(), *offset as u64)
                })
                .collect();
            bindings.push((x.buffer(), 0));
            bindings.push((products.buffer(), 0));
            self.launch_offsets(
                "routed_gate_up",
                &bindings,
                &params,
                addresses.len() * intermediate * 32,
                128,
            )?;
            bindings[8] = (products.buffer(), 0);
            bindings[9] = (projections.buffer(), 0);
            self.launch_offsets(
                "routed_down",
                &bindings,
                &params,
                addresses.len() * hidden * 32,
                128,
            )?;
            self.launch(
                "routed_mix",
                &[projections.buffer(), out.buffer()],
                &params,
                hidden,
                128,
            )?;
        }
        Ok(())
    }
}
