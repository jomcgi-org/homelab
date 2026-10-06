use std::sync::Arc;

use anyhow::Result;

/// Supplies routed-expert records on the device. Expert tiers implement it with
/// device slots over host memory over disk; the simplest source reads every record
/// from the model files.
///
/// Kernels read every part of a record, scales included, from the record itself, so
/// a source only hands out record addresses. Routing alone decides which experts
/// run; a source only decides where their records come from.
pub trait ExpertSource<B> {
    /// Makes `experts` of `layer` device-resident and returns the device address of
    /// each one's record, in the same order, usable by work issued afterwards.
    /// Addresses stay valid until the next fetch.
    fn fetch(&mut self, b: &B, layer: u32, experts: &[u32]) -> Result<Vec<u64>>;

    /// Two-phase fetch: returns every record's address and whether it is usable by
    /// work issued now. The rest become usable after
    /// [`ExpertSource::finish_fetch`], so callers can compute with resident experts
    /// while the others load.
    ///
    /// With `host_ok` the caller can compute experts on the host this step: the
    /// source may then leave some host-resident records where they are and return
    /// their host addresses in [`Staged::host`] instead of copying them to the
    /// device.
    fn begin_fetch(&mut self, b: &B, layer: u32, experts: &[u32], host_ok: bool) -> Result<Staged> {
        let _ = host_ok;
        let addrs = self.fetch(b, layer, experts)?;
        Ok(Staged {
            ready: vec![true; addrs.len()],
            host: vec![None; addrs.len()],
            addrs,
        })
    }

    /// Completes the last [`ExpertSource::begin_fetch`]: every record it returned is
    /// usable by work issued after this call.
    fn finish_fetch(&mut self, _b: &B) -> Result<()> {
        Ok(())
    }

    /// Hints that `layer` is about to route to `experts` (a prediction): the source
    /// may start loading them toward a faster tier. A wrong hint only costs
    /// bandwidth. Called after the previous fetch finished.
    fn prefetch(&mut self, _b: &B, _layer: u32, _experts: &[u32]) -> Result<()> {
        Ok(())
    }

    /// Whether [`ExpertSource::prefetch`] hints are used (so callers can skip
    /// computing them).
    fn wants_prefetch(&self) -> bool {
        false
    }

    /// Starts loading `experts` of `layer` (a prediction) into device memory ahead of
    /// that layer's next large fetch, as prefill does one layer ahead while the
    /// current layer computes. Records of the last fetched layer stay where they are.
    /// A wrong prediction only costs bandwidth: the fetch still loads what routing
    /// picks. Called after the previous fetch finished.
    fn stage_ahead(&mut self, _b: &B, _layer: u32, _experts: &[u32]) -> Result<()> {
        Ok(())
    }

    /// Queues the device copies of the disk reads [`ExpertSource::stage_ahead`]
    /// started that have completed, without waiting for the rest. Call it often
    /// while the device is busy, so copies overlap compute; the staged layer's
    /// fetch waits for whatever is left.
    fn finish_stage_ahead(&mut self, _b: &B) -> Result<()> {
        Ok(())
    }

    /// Whether [`ExpertSource::stage_ahead`] is used (so callers can skip predicting).
    fn stages_ahead(&self) -> bool {
        false
    }

    /// Gives device memory back so other buffers (e.g. a growing KV cache) can use
    /// it: frees at least `bytes` of cached records if possible and returns how many
    /// bytes it freed. Called between steps, never while a fetch is open.
    fn release_vram(&mut self, _b: &B, _bytes: usize) -> Result<usize> {
        Ok(0)
    }

    /// How much device memory [`Self::release_vram`] could free now.
    fn releasable_vram(&self) -> usize {
        0
    }

    /// Takes device memory back after it was released: grows the cache by up to
    /// `bytes` (never beyond its configured size) and returns how many bytes it took.
    fn reclaim_vram(&mut self, _b: &B, _bytes: usize) -> Result<usize> {
        Ok(0)
    }

    /// Where records came from so far.
    fn stats(&self) -> ExpertStats {
        ExpertStats::default()
    }
    /// How many records each tier holds (capacity, not occupancy).
    fn tiers(&self) -> ExpertTiers {
        ExpertTiers::default()
    }

    /// One line describing the source (sizes, policies).
    fn describe(&self) -> String;
}

/// Builds a model's expert source once its dense weights and first session state are
/// on the device, so a tiered source can size itself from what is left.
pub type ExpertFactory<B> = Box<dyn FnOnce(&Arc<B>) -> Result<Box<dyn ExpertSource<B>>>>;

/// Result of [`ExpertSource::begin_fetch`].
pub struct Staged {
    pub addrs: Vec<u64>,
    /// `ready[i]`: record `i` is usable before `finish_fetch`.
    pub ready: Vec<bool>,
    /// `host[i]`: record `i` stays in pinned host memory at this address and the
    /// caller computes it on the host (its `addrs[i]` is not a device record). The
    /// memory stays unchanged until the source's next fetch or prefetch.
    pub host: Vec<Option<usize>>,
}

/// Records already on the device, as returned by a finished fetch of one layer:
/// lets several steps of that layer use one fetch (prefill fetches the union of
/// its chunks' experts once).
pub struct Fetched {
    layer: u32,
    addrs: std::collections::HashMap<u32, u64>,
}

impl Fetched {
    /// `addrs[i]` is the record of `experts[i]` of `layer`.
    pub fn new(layer: u32, experts: &[u32], addrs: &[u64]) -> Self {
        Fetched {
            layer,
            addrs: experts.iter().copied().zip(addrs.iter().copied()).collect(),
        }
    }
}

impl<B> ExpertSource<B> for Fetched {
    fn fetch(&mut self, _b: &B, layer: u32, experts: &[u32]) -> Result<Vec<u64>> {
        anyhow::ensure!(
            layer == self.layer,
            "fetched records are for layer {}, not {layer}",
            self.layer
        );
        experts
            .iter()
            .map(|e| {
                self.addrs
                    .get(e)
                    .copied()
                    .ok_or_else(|| anyhow::anyhow!("expert {e} of layer {layer} was not fetched"))
            })
            .collect()
    }

    fn describe(&self) -> String {
        format!(
            "{} fetched records of layer {}",
            self.addrs.len(),
            self.layer
        )
    }
}

/// How many routed-expert records each tier of an [`ExpertSource`] holds.
#[derive(Debug, Default, Clone, Copy)]
pub struct ExpertTiers {
    /// Records the source serves, and their bytes.
    pub records: u64,
    pub bytes: u64,
    /// Records (and bytes) the VRAM tier and the pinned host tier hold.
    pub vram_records: u64,
    pub vram_bytes: u64,
    pub host_records: u64,
    pub host_bytes: u64,
}

impl std::ops::Add for ExpertTiers {
    type Output = ExpertTiers;
    fn add(self, o: ExpertTiers) -> ExpertTiers {
        ExpertTiers {
            records: self.records + o.records,
            bytes: self.bytes + o.bytes,
            vram_records: self.vram_records + o.vram_records,
            vram_bytes: self.vram_bytes + o.vram_bytes,
            host_records: self.host_records + o.host_records,
            host_bytes: self.host_bytes + o.host_bytes,
        }
    }
}

/// Counters of an [`ExpertSource`]; subtract two snapshots for an interval.
#[derive(Debug, Default, Clone, Copy)]
pub struct ExpertStats {
    pub requests: u64,
    pub vram_hits: u64,
    pub host_hits: u64,
    pub disk_reads: u64,
    /// Host hits computed on the host instead of copied to the device.
    pub host_computed: u64,
    /// Experts predicted for a layer, how many of those it then routed to, and how
    /// many experts it routed to in total (prediction precision and recall).
    pub predicted: u64,
    pub predicted_routed: u64,
    pub routed_after_prediction: u64,
    /// Disk reads started by predictions, and how many of them a fetch then used.
    pub lookahead_reads: u64,
    pub lookahead_used: u64,
    /// Records loaded toward the device by [`ExpertSource::stage_ahead`], and how
    /// many of them the next fetch of their layer then used.
    pub staged: u64,
    pub staged_used: u64,
}

impl std::ops::Add for ExpertStats {
    type Output = ExpertStats;
    fn add(self, o: ExpertStats) -> ExpertStats {
        ExpertStats {
            requests: self.requests + o.requests,
            vram_hits: self.vram_hits + o.vram_hits,
            host_hits: self.host_hits + o.host_hits,
            disk_reads: self.disk_reads + o.disk_reads,
            host_computed: self.host_computed + o.host_computed,
            predicted: self.predicted + o.predicted,
            predicted_routed: self.predicted_routed + o.predicted_routed,
            routed_after_prediction: self.routed_after_prediction + o.routed_after_prediction,
            lookahead_reads: self.lookahead_reads + o.lookahead_reads,
            lookahead_used: self.lookahead_used + o.lookahead_used,
            staged: self.staged + o.staged,
            staged_used: self.staged_used + o.staged_used,
        }
    }
}

impl std::ops::Sub for ExpertStats {
    type Output = ExpertStats;
    fn sub(self, o: ExpertStats) -> ExpertStats {
        ExpertStats {
            requests: self.requests - o.requests,
            vram_hits: self.vram_hits - o.vram_hits,
            host_hits: self.host_hits - o.host_hits,
            disk_reads: self.disk_reads - o.disk_reads,
            host_computed: self.host_computed - o.host_computed,
            predicted: self.predicted - o.predicted,
            predicted_routed: self.predicted_routed - o.predicted_routed,
            routed_after_prediction: self.routed_after_prediction - o.routed_after_prediction,
            lookahead_reads: self.lookahead_reads - o.lookahead_reads,
            lookahead_used: self.lookahead_used - o.lookahead_used,
            staged: self.staged - o.staged,
            staged_used: self.staged_used - o.staged_used,
        }
    }
}
