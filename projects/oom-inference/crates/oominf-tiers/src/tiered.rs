//! `TieredExperts`: VRAM slots over a pinned host tier over the model's
//! `experts.bin` (O_DIRECT via io_uring).
//!
//! A fetch has two phases. `begin_fetch` places every requested record, hands out
//! the addresses of those already in VRAM, enqueues host-tier hits on a dedicated
//! copy stream and submits disk reads for the rest, then returns, so the caller can
//! compute with the resident experts meanwhile. `finish_fetch` reaps the disk reads,
//! enqueues their copies as each lands, and makes the compute stream wait for the
//! copy stream.
//!
//! How the invariants of `specs/ExpertTiering.tla` hold here:
//! - **KernelReadsValid** (in-flight work reads fully staged memory of its expert):
//!   before a fetch's first copy, the copy stream waits on an event recorded on the
//!   compute stream, so a slot is refilled only after every kernel enqueued earlier
//!   (including those reading its previous contents) has run. Kernels that read the
//!   new contents are enqueued after `finish_fetch` makes the compute stream wait for
//!   the copies. Records requested by the current fetch are pinned against eviction
//!   while the others are placed, and resident records handed out early are never
//!   refilled by the same fetch.
//! - **CopySourceValid** (a copy's host source keeps its expert until it completes):
//!   every host slot records the sequence number of the last fetch whose copies read
//!   it, and each fetch with copies records an event on the copy stream. Before a disk
//!   read overwrites a host slot, the tier waits for that slot's last copy event.
//! - **TableConsistent**: the host-side slot table (`SlotCache`) is updated before the
//!   copy is enqueued, but nothing on the device reads a slot except kernels ordered
//!   after the copy, so no consumer can observe the gap. A device-side slot table for
//!   CUDA graphs must be written on the compute stream after it waits for the copies.
//! - **NoDuplicates**: `SlotCache` maps each key to at most one slot per tier.

use std::collections::{HashSet, VecDeque};
use std::sync::Arc;

use anyhow::{Context, Result, ensure};
use cudarc::driver::{CudaEvent, CudaStream, sys};
use oominf_cuda::{Gpu, Slice};
use oominf_format::Model;
use oominf_models_qwen::{ExpertSource, Staged};

use crate::cache::{Place, SlotCache};
use crate::host::{DirectReader, PinnedArena, ReadJob};
use crate::policy::Policy;

#[derive(Debug, Default, Clone, Copy)]
pub struct TierStats {
    pub requests: u64,
    pub vram_hits: u64,
    pub host_hits: u64,
    pub disk_reads: u64,
}

impl std::ops::Sub for TierStats {
    type Output = TierStats;
    fn sub(self, o: TierStats) -> TierStats {
        TierStats {
            requests: self.requests - o.requests,
            vram_hits: self.vram_hits - o.vram_hits,
            host_hits: self.host_hits - o.host_hits,
            disk_reads: self.disk_reads - o.disk_reads,
        }
    }
}

const GIB: f64 = (1u64 << 30) as f64;

pub struct TieredExperts {
    model: Arc<Model>,
    num_experts: u32,
    stride: usize,
    vram: SlotCache,
    vram_arena: Slice<u8>,
    vram_base: u64,
    host: SlotCache,
    host_arena: PinnedArena,
    /// Fetch sequence number of the last device copy that read each host slot.
    host_last_copy: Vec<u64>,
    /// Copy-stream events of fetches whose copies may still be pending, oldest first.
    pending: VecDeque<(u64, CudaEvent)>,
    copy_stream: Arc<CudaStream>,
    reader: DirectReader,
    /// Disk reads of the open fetch: (host slot, VRAM slot, key) per read tag.
    reads: Vec<(usize, usize, u32)>,
    /// The open fetch enqueued copies that the compute stream has not waited for.
    open: bool,
    seq: u64,
    pub stats: TierStats,
}

impl TieredExperts {
    /// `vram_slots` and `host_slots` are record counts; see [`TieredExperts::slots_for`].
    pub fn new(
        gpu: &Gpu,
        model: Arc<Model>,
        vram_slots: usize,
        host_slots: usize,
        vram_policy: Box<dyn Policy>,
        host_policy: Box<dyn Policy>,
    ) -> Result<Self> {
        let groups = &model.index().expert_groups;
        let first = groups.first().context("model has no expert groups")?;
        let stride = first.schema.stride as usize;
        let num_experts = first.num_experts;
        ensure!(
            groups
                .iter()
                .all(|g| g.schema.stride as usize == stride && g.num_experts == num_experts),
            "expert groups differ in stride or expert count"
        );
        ensure!(
            vram_slots > 0 && host_slots > 0,
            "tier sizes must be positive"
        );
        let vram_arena = gpu.stream.alloc_zeros::<u8>(vram_slots * stride)?;
        let vram_base = gpu.device_ptr(&vram_arena);
        gpu.ctx.bind_to_thread()?;
        let host_arena = PinnedArena::new(host_slots, stride)?;
        let reader = DirectReader::open(&model.dir().join(oominf_format::EXPERTS_FILE))?;
        Ok(TieredExperts {
            num_experts,
            stride,
            vram: SlotCache::new(vram_slots, vram_policy),
            vram_arena,
            vram_base,
            host: SlotCache::new(host_slots, host_policy),
            host_last_copy: vec![0; host_slots],
            host_arena,
            pending: VecDeque::new(),
            copy_stream: gpu.ctx.new_stream()?,
            reader,
            reads: Vec::new(),
            open: false,
            seq: 0,
            stats: TierStats::default(),
            model,
        })
    }

    /// Records that fit in `gib` GiB for this model.
    pub fn slots_for(model: &Model, gib: f64) -> usize {
        let stride = model
            .index()
            .expert_groups
            .first()
            .map(|g| g.schema.stride)
            .unwrap_or(1);
        ((gib * GIB) / stride as f64) as usize
    }

    /// Free device memory now, minus `reserve_gib`, in GiB (at least 0).
    pub fn free_vram_gib(gpu: &Gpu, reserve_gib: f64) -> Result<f64> {
        let (free, _) = gpu.ctx.mem_get_info()?;
        Ok((free as f64 / GIB - reserve_gib).max(0.0))
    }

    /// `MemAvailable` from `/proc/meminfo`, minus `reserve_gib`, in GiB (at least 0).
    pub fn available_host_gib(reserve_gib: f64) -> Result<f64> {
        let info = std::fs::read_to_string("/proc/meminfo")?;
        let kib: f64 = info
            .lines()
            .find_map(|l| l.strip_prefix("MemAvailable:"))
            .and_then(|v| v.trim().trim_end_matches("kB").trim().parse().ok())
            .context("MemAvailable missing from /proc/meminfo")?;
        Ok((kib / (1u64 << 20) as f64 - reserve_gib).max(0.0))
    }

    pub fn describe(&self) -> String {
        format!(
            "tiered experts: VRAM {} slots ({:.1} GiB, {}), host {} slots ({:.1} GiB pinned, {})",
            self.vram.capacity(),
            self.vram_arena.len() as f64 / GIB,
            self.vram.policy_name(),
            self.host.capacity(),
            (self.host.capacity() * self.stride) as f64 / GIB,
            self.host.policy_name()
        )
    }

    /// Blocks until every copy that read host slot `slot` has completed.
    fn wait_host_slot(&mut self, slot: usize) -> Result<()> {
        let need = self.host_last_copy[slot];
        while let Some((s, _)) = self.pending.front() {
            if *s > need {
                break;
            }
            let (_, ev) = self.pending.pop_front().unwrap();
            ev.synchronize()?;
        }
        Ok(())
    }

    /// Enqueues the copy of host slot `host_slot` into VRAM slot `vram_slot` on the
    /// copy stream.
    fn copy_to_vram(&mut self, host_slot: usize, vram_slot: usize) -> Result<()> {
        enqueue_copy(
            &self.copy_stream,
            self.vram_base + (vram_slot * self.stride) as u64,
            &self.host_arena,
            host_slot,
        )?;
        self.host_last_copy[host_slot] = self.seq;
        Ok(())
    }

    fn forget_reads(&mut self) {
        for &(_, _, key) in &self.reads {
            self.host.forget(key);
            self.vram.forget(key);
        }
        self.reads.clear();
    }
}

/// Enqueues one record copy from a host slot to device address `dst` on `stream`.
fn enqueue_copy(
    stream: &CudaStream,
    dst: u64,
    arena: &PinnedArena,
    host_slot: usize,
) -> Result<()> {
    // SAFETY: dst is a slot of the VRAM arena that no kernel ordered before the copy
    // reads (KernelReadsValid); the source is a registered host slot that is not
    // overwritten until this fetch's copy event completes (CopySourceValid).
    unsafe {
        cudarc::driver::result::memcpy_htod_async(dst, arena.slot(host_slot), stream.cu_stream())?;
    }
    Ok(())
}

impl ExpertSource for TieredExperts {
    fn fetch(&mut self, gpu: &Gpu, layer: u32, experts: &[u32]) -> Result<Vec<u64>> {
        let staged = self.begin_fetch(gpu, layer, experts)?;
        self.finish_fetch(gpu)?;
        Ok(staged.addrs)
    }

    fn begin_fetch(&mut self, gpu: &Gpu, layer: u32, experts: &[u32]) -> Result<Staged> {
        self.finish_fetch(gpu)?;
        self.seq += 1;
        let keys: Vec<u32> = experts
            .iter()
            .map(|&e| {
                ensure!(e < self.num_experts, "expert {e} out of range");
                Ok(layer * self.num_experts + e)
            })
            .collect::<Result<_>>()?;
        let wanted: HashSet<u32> = keys.iter().copied().collect();
        let pinned = |k: u32| wanted.contains(&k);

        // VRAM placement.
        let mut addrs = Vec::with_capacity(keys.len());
        let mut ready = Vec::with_capacity(keys.len());
        let mut fills = Vec::new(); // (key, vram slot)
        for &key in &keys {
            self.stats.requests += 1;
            let place = self
                .vram
                .place(key, &pinned)
                .context("VRAM expert tier is smaller than one layer's routed experts")?;
            let slot = match place {
                Place::Hit(s) => {
                    self.stats.vram_hits += 1;
                    s
                }
                Place::Miss(s, _) => {
                    fills.push((key, s));
                    s
                }
            };
            ready.push(matches!(place, Place::Hit(_)));
            addrs.push(self.vram_base + (slot * self.stride) as u64);
        }
        if fills.is_empty() {
            return Ok(Staged { addrs, ready });
        }

        // Refills wait for every kernel enqueued so far (KernelReadsValid).
        let after = gpu
            .stream
            .record_event(Some(sys::CUevent_flags::CU_EVENT_DISABLE_TIMING))?;
        self.copy_stream.wait(&after)?;
        self.open = true;

        // Host placement for the VRAM misses: host hits copy now, the rest read from disk.
        let mut jobs = Vec::new();
        for (key, vslot) in fills {
            let place = self
                .host
                .place(key, &pinned)
                .context("host expert tier is smaller than one layer's routed experts")?;
            match place {
                Place::Hit(hs) => {
                    self.stats.host_hits += 1;
                    self.copy_to_vram(hs, vslot)?;
                }
                Place::Miss(hs, _) => {
                    self.stats.disk_reads += 1;
                    self.wait_host_slot(hs)?;
                    let (l, e) = (key / self.num_experts, key % self.num_experts);
                    let (offset, stride) = self.model.record_location(l, e)?;
                    jobs.push(ReadJob {
                        offset,
                        dst: self.host_arena.slot_ptr(hs),
                        len: stride as usize,
                        tag: self.reads.len(),
                    });
                    self.reads.push((hs, vslot, key));
                }
            }
        }
        if !jobs.is_empty()
            && let Err(e) = self.reader.submit(jobs)
        {
            self.forget_reads();
            return Err(e);
        }
        Ok(Staged { addrs, ready })
    }

    fn finish_fetch(&mut self, gpu: &Gpu) -> Result<()> {
        if !self.open {
            return Ok(());
        }
        self.open = false;
        let reads = std::mem::take(&mut self.reads);
        let (seq, base, stride) = (self.seq, self.vram_base, self.stride);
        let (stream, arena, last) = (
            &self.copy_stream,
            &self.host_arena,
            &mut self.host_last_copy,
        );
        let drained = self.reader.drain(|tag| {
            let (hs, vs, _) = reads[tag];
            enqueue_copy(stream, base + (vs * stride) as u64, arena, hs)?;
            last[hs] = seq;
            Ok(())
        });
        if let Err(e) = drained {
            self.reads = reads;
            self.forget_reads();
            return Err(e);
        }
        let done = self
            .copy_stream
            .record_event(Some(sys::CUevent_flags::CU_EVENT_DISABLE_TIMING))?;
        gpu.stream.wait(&done)?;
        self.pending.push_back((self.seq, done));
        // Drop completed events so the queue stays short.
        while self.pending.len() > 1 {
            let complete = unsafe { sys::cuEventQuery(self.pending[0].1.cu_event()) }
                == sys::cudaError_enum::CUDA_SUCCESS;
            if !complete {
                break;
            }
            self.pending.pop_front();
        }
        Ok(())
    }
}
