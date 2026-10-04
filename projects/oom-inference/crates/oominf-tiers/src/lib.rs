//! Expert storage tiers: device slots over a pinned host tier over disk, for any
//! backend and any model whose experts are stored as records.
//!
//! [`TieredExperts`] and [`DiskExperts`] implement
//! [`ExpertSource`](oominf_core::ExpertSource). Placement is device-free
//! ([`cache::SlotCache`] plus a [`policy::Policy`]), so the same code also replays
//! recorded routing traces (`cargo run -p oominf-tiers --example replay`).

pub mod cache;
mod disk;
pub mod host;
pub mod policy;
mod tiered;

pub use disk::DiskExperts;
pub use tiered::{TieredExperts, available_host_gib, free_vram_gib, slots_for};

/// A recorded decode routing trace: raw little-endian `u16` `[steps, layers, top_k]`.
pub fn load_trace(
    path: &std::path::Path,
    layers: usize,
    top_k: usize,
) -> anyhow::Result<Vec<Vec<Vec<u16>>>> {
    let bytes = std::fs::read(path)?;
    let per_step = layers * top_k * 2;
    anyhow::ensure!(
        bytes.len() % per_step == 0,
        "trace is not [steps, {layers}, {top_k}] u16"
    );
    Ok(bytes
        .chunks_exact(per_step)
        .map(|step| {
            step.chunks_exact(top_k * 2)
                .map(|l| {
                    l.as_chunks::<2>()
                        .0
                        .iter()
                        .map(|&b| u16::from_le_bytes(b))
                        .collect()
                })
                .collect()
        })
        .collect())
}
