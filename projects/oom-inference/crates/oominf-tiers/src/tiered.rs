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
//!
//! Large fetches (prefill: more than `stream_threshold` experts at once) stream:
//! records already in the VRAM tier are used without counting an access, and the
//! rest take free main slots while any remain, then go to a separate VRAM stage
//! arena (one layer's worth of slots, LRU) filled from host-tier hits, free host
//! slots or a pinned host stage ring. Prefill therefore fills empty tiers but never
//! evicts or re-ranks the decode-hot tiers. A later decode miss whose record is still staged
//! is promoted with a device-to-device copy. The stage follows the same rules: its
//! slots are refilled on the copy stream after the compute-stream event, and a host
//! stage slot is reused only after the copy that read it has completed.

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

/// Where a pending copy reads from.
#[derive(Clone, Copy)]
enum Src {
    /// A host-tier slot.
    Host(usize),
    /// A host stage ring slot.
    Stage(usize),
}

/// Which VRAM cache a pending fill belongs to.
#[derive(Clone, Copy)]
enum Target {
    Main,
    Stage,
}

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
    /// Main slots then stage slots, `stride` bytes each.
    vram_arena: Slice<u8>,
    vram_base: u64,
    /// Streaming fetches fill these instead of the main VRAM tier (empty when the
    /// VRAM budget is too small to set a layer's worth aside).
    stage: SlotCache,
    host: SlotCache,
    host_arena: PinnedArena,
    /// Fetch sequence number of the last device copy that read each host slot.
    host_last_copy: Vec<u64>,
    host_stage: PinnedArena,
    host_stage_last_copy: Vec<u64>,
    host_stage_next: usize,
    /// Fetches with more experts than this stream through the stage.
    stream_threshold: usize,
    /// Copy-stream events of fetches whose copies may still be pending, oldest first.
    pending: VecDeque<(u64, CudaEvent)>,
    copy_stream: Arc<CudaStream>,
    reader: DirectReader,
    /// Disk reads of the open fetch, per read tag: (host buffer, device address,
    /// key, VRAM cache the key was placed in).
    reads: Vec<(Src, u64, u32, Target)>,
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
        // Set one layer's worth of slots aside for streaming when the budget allows.
        let stage_slots = if vram_slots >= 2 * num_experts as usize {
            num_experts as usize
        } else {
            0
        };
        let main_slots = vram_slots - stage_slots;
        let vram_arena = gpu.stream.alloc_zeros::<u8>(vram_slots * stride)?;
        let vram_base = gpu.device_ptr(&vram_arena);
        gpu.ctx.bind_to_thread()?;
        let host_arena = PinnedArena::new(host_slots, stride)?;
        let host_stage = PinnedArena::new(stage_slots.max(1), stride)?;
        let reader = DirectReader::open(&model.dir().join(oominf_format::EXPERTS_FILE))?;
        Ok(TieredExperts {
            num_experts,
            stride,
            vram: SlotCache::new(main_slots, vram_policy),
            vram_arena,
            vram_base,
            stage: SlotCache::new(stage_slots, Box::new(crate::policy::Lru::default())),
            host: SlotCache::new(host_slots, host_policy),
            host_last_copy: vec![0; host_slots],
            host_arena,
            host_stage_last_copy: vec![0; stage_slots.max(1)],
            host_stage,
            host_stage_next: 0,
            stream_threshold: (num_experts as usize / 8).max(1),
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
            "tiered experts: VRAM {} + {} stage slots ({:.1} GiB, {}), host {} slots ({:.1} GiB pinned, {})",
            self.vram.capacity(),
            self.stage.capacity(),
            self.vram_arena.len() as f64 / GIB,
            self.vram.policy_name(),
            self.host.capacity(),
            (self.host.capacity() * self.stride) as f64 / GIB,
            self.host.policy_name()
        )
    }

    /// Device address of main VRAM slot `slot`.
    fn main_addr(&self, slot: usize) -> u64 {
        self.vram_base + (slot * self.stride) as u64
    }

    /// Device address of stage slot `slot` (stage slots follow the main slots).
    fn stage_addr(&self, slot: usize) -> u64 {
        self.main_addr(self.vram.capacity() + slot)
    }

    /// Blocks until every copy enqueued by fetches up to `need` has completed.
    fn wait_copies(&mut self, need: u64) -> Result<()> {
        while let Some((s, _)) = self.pending.front() {
            if *s > need {
                break;
            }
            let (_, ev) = self.pending.pop_front().unwrap();
            ev.synchronize()?;
        }
        Ok(())
    }

    /// Enqueues the copy of host slot `host_slot` to device address `dst` on the
    /// copy stream.
    fn copy_host(&mut self, host_slot: usize, dst: u64) -> Result<()> {
        enqueue_copy(&self.copy_stream, dst, &self.host_arena, host_slot)?;
        self.host_last_copy[host_slot] = self.seq;
        Ok(())
    }

    /// Next host stage ring slot, once the copy that last read it has completed.
    fn next_host_stage(&mut self) -> Result<usize> {
        let slot = self.host_stage_next;
        self.host_stage_next = (slot + 1) % self.host_stage_last_copy.len();
        self.wait_copies(self.host_stage_last_copy[slot])?;
        Ok(slot)
    }

    /// Queues a disk read of `key` into `src` that will then be copied to `dst`.
    fn queue_read(
        &mut self,
        jobs: &mut Vec<ReadJob>,
        key: u32,
        src: Src,
        dst: u64,
        target: Target,
    ) -> Result<()> {
        self.stats.disk_reads += 1;
        let (l, e) = (key / self.num_experts, key % self.num_experts);
        let (offset, stride) = self.model.record_location(l, e)?;
        let buf = match src {
            Src::Host(hs) => self.host_arena.slot_ptr(hs),
            Src::Stage(ss) => self.host_stage.slot_ptr(ss),
        };
        jobs.push(ReadJob {
            offset,
            dst: buf,
            len: stride as usize,
            tag: self.reads.len(),
        });
        self.reads.push((src, dst, key, target));
        Ok(())
    }

    fn forget_reads(&mut self) {
        for &(src, _, key, target) in &self.reads {
            if let Src::Host(_) = src {
                self.host.forget(key);
            }
            match target {
                Target::Main => self.vram.forget(key),
                Target::Stage => self.stage.forget(key),
            }
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
        let streaming = self.stage.capacity() > 0 && keys.len() > self.stream_threshold;

        // VRAM placement: addresses now, fills (key, target, address) and
        // device-to-device promotions (source, destination) for later.
        let mut addrs = Vec::with_capacity(keys.len());
        let mut ready = Vec::with_capacity(keys.len());
        let mut fills = Vec::new();
        let mut promotions = Vec::new();
        for &key in &keys {
            self.stats.requests += 1;
            if streaming {
                if let Some(s) = self.vram.peek(key) {
                    self.stats.vram_hits += 1;
                    addrs.push(self.main_addr(s));
                    ready.push(true);
                    continue;
                }
                if self.vram.has_free() && self.stage.peek(key).is_none() {
                    let Some(Place::Miss(s, None)) = self.vram.place(key, &pinned) else {
                        unreachable!("a free slot evicts nothing");
                    };
                    let dst = self.main_addr(s);
                    fills.push((key, Target::Main, dst));
                    addrs.push(dst);
                    ready.push(false);
                    continue;
                }
                let place = self
                    .stage
                    .place(key, &pinned)
                    .context("VRAM stage is smaller than one step's routed experts")?;
                match place {
                    Place::Hit(s) => {
                        self.stats.vram_hits += 1;
                        addrs.push(self.stage_addr(s));
                        ready.push(true);
                    }
                    Place::Miss(s, _) => {
                        let dst = self.stage_addr(s);
                        fills.push((key, Target::Stage, dst));
                        addrs.push(dst);
                        ready.push(false);
                    }
                }
                continue;
            }
            let place = self
                .vram
                .place(key, &pinned)
                .context("VRAM expert tier is smaller than one layer's routed experts")?;
            match place {
                Place::Hit(s) => {
                    self.stats.vram_hits += 1;
                    addrs.push(self.main_addr(s));
                    ready.push(true);
                }
                Place::Miss(s, _) => {
                    let dst = self.main_addr(s);
                    match self.stage.peek(key) {
                        Some(ss) => {
                            self.stats.vram_hits += 1;
                            promotions.push((self.stage_addr(ss), dst));
                        }
                        None => fills.push((key, Target::Main, dst)),
                    }
                    addrs.push(dst);
                    ready.push(false);
                }
            }
        }
        if fills.is_empty() && promotions.is_empty() {
            return Ok(Staged { addrs, ready });
        }

        // Refills wait for every kernel enqueued so far (KernelReadsValid).
        let after = gpu
            .stream
            .record_event(Some(sys::CUevent_flags::CU_EVENT_DISABLE_TIMING))?;
        self.copy_stream.wait(&after)?;
        self.open = true;
        for (src, dst) in promotions {
            // SAFETY: both are slots of the VRAM arena; the source is only refilled by
            // later copies on this same stream, and the destination is read only by
            // kernels ordered after finish_fetch.
            unsafe {
                cudarc::driver::result::memcpy_dtod_async(
                    dst,
                    src,
                    self.stride,
                    self.copy_stream.cu_stream(),
                )?;
            }
        }

        // Host side of each fill: host hits copy now, the rest read from disk.
        let mut jobs = Vec::new();
        for (key, target, dst) in fills {
            if streaming {
                match self.host.peek(key) {
                    Some(hs) => {
                        self.stats.host_hits += 1;
                        self.copy_host(hs, dst)?;
                    }
                    None if self.host.has_free() => {
                        let Some(Place::Miss(hs, None)) = self.host.place(key, &pinned) else {
                            unreachable!("a free slot evicts nothing");
                        };
                        self.queue_read(&mut jobs, key, Src::Host(hs), dst, target)?;
                    }
                    None => {
                        let ss = self.next_host_stage()?;
                        self.queue_read(&mut jobs, key, Src::Stage(ss), dst, target)?;
                    }
                }
                continue;
            }
            let place = self
                .host
                .place(key, &pinned)
                .context("host expert tier is smaller than one layer's routed experts")?;
            match place {
                Place::Hit(hs) => {
                    self.stats.host_hits += 1;
                    self.copy_host(hs, dst)?;
                }
                Place::Miss(hs, _) => {
                    self.wait_copies(self.host_last_copy[hs])?;
                    self.queue_read(&mut jobs, key, Src::Host(hs), dst, target)?;
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
        let seq = self.seq;
        let (stream, host, host_last, stage, stage_last) = (
            &self.copy_stream,
            &self.host_arena,
            &mut self.host_last_copy,
            &self.host_stage,
            &mut self.host_stage_last_copy,
        );
        let drained = self.reader.drain(|tag| {
            let (src, dst, _, _) = reads[tag];
            match src {
                Src::Host(hs) => {
                    enqueue_copy(stream, dst, host, hs)?;
                    host_last[hs] = seq;
                }
                Src::Stage(ss) => {
                    enqueue_copy(stream, dst, stage, ss)?;
                    stage_last[ss] = seq;
                }
            }
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
