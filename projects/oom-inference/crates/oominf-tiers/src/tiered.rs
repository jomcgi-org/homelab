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
//!
//! **Host compute** (decode-sized fetches with `host_ok`): a record that misses VRAM
//! but sits in the host tier can stay there and be computed on the CPU, up to
//! `host_compute` records per fetch; its host address goes back in
//! [`Staged::host`] and it is neither placed in VRAM nor copied. Admission to VRAM
//! is second-hit: the first host-tier miss of a key within the admission window is
//! computed on the host, a repeat is copied to VRAM, so one-off experts do not evict
//! recurring ones. **HostRecordValid**: a handed-out host record is pinned against
//! eviction for the rest of its fetch (it is one of the fetch's keys) and nothing
//! writes host slots between fetches except the next fetch or prefetch, which the
//! caller only issues after its host compute finished.
//!
//! **Lookahead** ([`ExpertSource::prefetch`], decode only): the model predicts the
//! next layer's experts and the tier reads predicted disk misses into host cache
//! slots (evicting cold residents, never VRAM residents, and only after the copy
//! that last read a slot completed). The next fetch drains those reads before it
//! copies anything, so a host slot is only a copy source once its read landed.
//!
//! **Victim cache** (decode): a decode fetch that evicts a main VRAM record first
//! copies it device to device into the stage (one copy-queue operation before the
//! slot's refill, after the compute-stream event like every refill), so the stage
//! extends the decode cache with recently evicted records between prefills.
//!
//! **Giving VRAM back** ([`ExpertSource::release_vram`] / `reclaim_vram`): the main
//! arena is allocated in chunks of `CHUNK_SLOTS` records. A chunk is freed only after
//! the open fetch finished, every copy completed and the compute stream synchronised,
//! so no kernel, copy or table entry can still reference its slots (the spec's
//! `Retire`); reclaimed chunks come back empty (`Restore`).

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::Arc;

use anyhow::{Context, Result, ensure};
use oominf_core::{Backend, ExpertSource, ExpertStats, Memory, Staged};
use oominf_format::Model;

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

const GIB: f64 = (1u64 << 30) as f64;

/// Records per chunk of the main VRAM arena (about 177 MB for Qwen 3.8 Flash).
/// Records read per batch when preloading the host tier.
const PRELOAD_BATCH: usize = 64;

pub const CHUNK_SLOTS: usize = 64;

/// Fetches within which a second host-tier miss of a key admits it to VRAM (about
/// 32 decode tokens of a 48-layer model).
const ADMIT_WINDOW: u64 = 48 * 32;

pub struct TieredExperts<B: Backend> {
    b: Arc<B>,
    model: Arc<Model>,
    /// The expert groups (layers) this source serves.
    layers: Vec<u32>,
    num_experts: u32,
    stride: usize,
    vram: SlotCache,
    /// The main VRAM tier in chunks of `CHUNK_SLOTS` records, so whole chunks can be
    /// given back ([`ExpertSource::release_vram`]) and taken again
    /// ([`ExpertSource::reclaim_vram`]) up to `max_chunks`.
    main_chunks: Vec<B::Bytes>,
    main_bases: Vec<u64>,
    max_chunks: usize,
    /// Owns the stage slots behind `stage_base`, `stride` bytes each (absent when
    /// there is no stage).
    _stage_arena: Option<B::Bytes>,
    stage_base: u64,
    /// Streaming fetches fill these instead of the main VRAM tier (empty when the
    /// VRAM budget is too small to set a layer's worth aside).
    stage: SlotCache,
    host: SlotCache,
    host_arena: PinnedArena<B>,
    /// Fetch sequence number of the last device copy that read each host slot.
    host_last_copy: Vec<u64>,
    host_stage: PinnedArena<B>,
    host_stage_last_copy: Vec<u64>,
    host_stage_next: usize,
    /// Fetches with more experts than this stream through the stage.
    stream_threshold: usize,
    /// Copy-stream events of fetches whose copies may still be pending, oldest first.
    pending: VecDeque<(u64, B::Event)>,
    copy_queue: B::CopyQueue,
    reader: DirectReader,
    /// Disk reads of the open fetch, per read tag: (host buffer, device address,
    /// key, VRAM cache the key was placed in).
    reads: Vec<(Src, u64, u32, Target)>,
    /// The open fetch enqueued copies that the compute stream has not waited for.
    open: bool,
    /// Start disk-to-host reads for predicted experts ([`ExpertSource::prefetch`]).
    pub lookahead: bool,
    /// Most records per fetch left in host memory for the caller to compute on the
    /// host (with `host_ok`); 0 copies every host-tier hit to VRAM.
    pub host_compute: usize,
    /// Fetch sequence number of each key's last host-computed miss (second-hit
    /// admission).
    host_seen: HashMap<u32, u64>,
    /// The last prediction: its layer and keys, and the keys whose disk reads it
    /// started (in flight until the next fetch drains them).
    prediction: Option<(u32, HashSet<u32>)>,
    lookahead_inflight: Vec<u32>,
    seq: u64,
    pub stats: ExpertStats,
}

impl<B: Backend> TieredExperts<B> {
    /// Serves the expert groups whose records have layout `layout` (all records of
    /// one layout share a size). `vram_slots` and `host_slots` are record counts; see
    /// [`slots_for`].
    pub fn new(
        b: Arc<B>,
        model: Arc<Model>,
        layout: &str,
        vram_slots: usize,
        host_slots: usize,
        vram_policy: Box<dyn Policy>,
        host_policy: Box<dyn Policy>,
    ) -> Result<Self> {
        let groups: Vec<_> = model
            .index()
            .expert_groups
            .iter()
            .filter(|g| g.schema.layout == layout)
            .collect();
        let first = groups
            .first()
            .with_context(|| format!("model has no {layout} expert groups"))?;
        let stride = first.schema.stride as usize;
        let num_experts = first.num_experts;
        ensure!(
            groups
                .iter()
                .all(|g| g.schema.stride as usize == stride && g.num_experts == num_experts),
            "{layout} expert groups differ in stride or expert count"
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
        let max_chunks = (vram_slots - stage_slots) / CHUNK_SLOTS;
        ensure!(
            max_chunks > 0,
            "VRAM tier of {vram_slots} slots is smaller than one {CHUNK_SLOTS}-slot chunk plus the stage"
        );
        let main_slots = max_chunks * CHUNK_SLOTS;
        let main_chunks = (0..max_chunks)
            .map(|_| b.zeros_bytes(CHUNK_SLOTS * stride))
            .collect::<Result<Vec<_>>>()?;
        let main_bases = main_chunks.iter().map(|c| b.bytes_addr(c)).collect();
        let stage_arena = if stage_slots > 0 {
            Some(b.zeros_bytes(stage_slots * stride)?)
        } else {
            None
        };
        let stage_base = stage_arena.as_ref().map_or(0, |a| b.bytes_addr(a));
        let host_arena = PinnedArena::new(b.clone(), host_slots, stride)?;
        let host_stage = PinnedArena::new(b.clone(), stage_slots.max(1), stride)?;
        let reader = DirectReader::open(&model.dir().join(oominf_format::EXPERTS_FILE))?;
        let layers = groups.iter().map(|g| g.layer).collect();
        Ok(TieredExperts {
            layers,
            num_experts,
            stride,
            vram: SlotCache::new(main_slots, vram_policy),
            main_chunks,
            main_bases,
            max_chunks,
            _stage_arena: stage_arena,
            stage_base,
            stage: SlotCache::new(stage_slots, Box::new(crate::policy::Lru::default())),
            host: SlotCache::new(host_slots, host_policy),
            host_last_copy: vec![0; host_slots],
            host_arena,
            host_stage_last_copy: vec![0; stage_slots.max(1)],
            host_stage,
            host_stage_next: 0,
            stream_threshold: (num_experts as usize / 8).max(1),
            pending: VecDeque::new(),
            copy_queue: b.copy_queue()?,
            reader,
            reads: Vec::new(),
            open: false,
            lookahead: false,
            host_compute: 0,
            host_seen: HashMap::new(),
            prediction: None,
            lookahead_inflight: Vec::new(),
            seq: 0,
            stats: ExpertStats::default(),
            model,
            b,
        })
    }

    /// Reads every record this source serves into the host tier when it can hold
    /// them all (e.g. a small group such as a draft head's experts, which no prompt
    /// warms). Returns how many records were read.
    pub fn preload_host(&mut self) -> Result<usize> {
        let total = self.layers.len() * self.num_experts as usize;
        if self.host.capacity() < total {
            return Ok(0);
        }
        let mut read = 0;
        let ne = self.num_experts;
        let keys: Vec<u32> = self
            .layers
            .iter()
            .flat_map(|&l| (0..ne).map(move |e| l * ne + e))
            .collect();
        for batch in keys.chunks(PRELOAD_BATCH) {
            let mut jobs = Vec::with_capacity(batch.len());
            for &key in batch {
                let Some(Place::Miss(hs, _)) = self.host.place(key, &|_| false) else {
                    continue;
                };
                let (l, e) = (key / self.num_experts, key % self.num_experts);
                let (offset, stride) = self.model.record_location(l, e)?;
                jobs.push(ReadJob {
                    offset,
                    dst: self.host_arena.slot_ptr(hs),
                    len: stride as usize,
                    tag: jobs.len(),
                });
            }
            read += jobs.len();
            self.reader.submit(jobs)?;
            self.reader.drain(|_| Ok(()))?;
        }
        Ok(read)
    }

    fn summary(&self) -> String {
        format!(
            "tiered experts: VRAM {} + {} stage slots ({:.1} GiB, {}), host {} slots ({:.1} GiB pinned, {})",
            self.vram.capacity(),
            self.stage.capacity(),
            ((self.vram.capacity() + self.stage.capacity()) * self.stride) as f64 / GIB,
            self.vram.policy_name(),
            self.host.capacity(),
            (self.host.capacity() * self.stride) as f64 / GIB,
            self.host.policy_name()
        )
    }

    /// Device address of main VRAM slot `slot`.
    fn main_addr(&self, slot: usize) -> u64 {
        self.main_bases[slot / CHUNK_SLOTS] + ((slot % CHUNK_SLOTS) * self.stride) as u64
    }

    /// Device address of stage slot `slot`.
    fn stage_addr(&self, slot: usize) -> u64 {
        self.stage_base + (slot * self.stride) as u64
    }

    /// Bytes of device memory the VRAM tier holds.
    pub fn vram_bytes(&self) -> usize {
        (self.vram.capacity() + self.stage.capacity()) * self.stride
    }

    /// Blocks until every copy enqueued by fetches up to `need` has completed.
    fn wait_copies(&mut self, need: u64) -> Result<()> {
        while let Some((s, _)) = self.pending.front() {
            if *s > need {
                break;
            }
            let (_, ev) = self.pending.pop_front().unwrap();
            self.b.event_wait(&ev)?;
        }
        Ok(())
    }

    /// Enqueues the copy of host slot `host_slot` to device address `dst` on the
    /// copy stream.
    fn copy_host(&mut self, host_slot: usize, dst: u64) -> Result<()> {
        enqueue_copy(&*self.b, &self.copy_queue, dst, &self.host_arena, host_slot)?;
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

    /// Waits for the lookahead reads (their records are host-resident from then on)
    /// and scores the last prediction against `experts`, the routing of `layer`.
    fn finish_lookahead(&mut self, layer: u32, experts: &[u32]) -> Result<()> {
        if !self.lookahead_inflight.is_empty()
            && let Err(e) = self.reader.drain(|_| Ok(()))
        {
            for k in self.lookahead_inflight.drain(..) {
                self.host.forget(k);
            }
            return Err(e);
        }
        let inflight = std::mem::take(&mut self.lookahead_inflight);
        if let Some((pl, keys)) = self.prediction.take()
            && pl == layer
        {
            let routed: HashSet<u32> = experts
                .iter()
                .map(|&e| layer * self.num_experts + e)
                .collect();
            self.stats.routed_after_prediction += routed.len() as u64;
            self.stats.predicted_routed += keys.intersection(&routed).count() as u64;
            self.stats.lookahead_used +=
                inflight.iter().filter(|k| routed.contains(k)).count() as u64;
        }
        Ok(())
    }

    /// Second-hit admission: whether a host-tier miss of `key` should enter VRAM
    /// now (it missed within the admission window before) rather than be computed
    /// on the host. Records this miss either way.
    fn admit(&mut self, key: u32) -> bool {
        let repeat = self
            .host_seen
            .insert(key, self.seq)
            .is_some_and(|last| self.seq - last <= ADMIT_WINDOW);
        if repeat {
            self.host_seen.remove(&key);
        }
        repeat
    }

    /// Drops admission history older than the window (bounded memory).
    fn prune_host_seen(&mut self) {
        if self.host_seen.len() > 4 * self.vram.capacity().max(1) {
            let seq = self.seq;
            self.host_seen
                .retain(|_, &mut last| seq - last <= ADMIT_WINDOW);
        }
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

/// Records of layout `layout` that fit in `gib` GiB for this model.
pub fn slots_for(model: &Model, layout: &str, gib: f64) -> usize {
    let stride = model
        .index()
        .expert_groups
        .iter()
        .find(|g| g.schema.layout == layout)
        .map_or(1, |g| g.schema.stride);
    ((gib * GIB) / stride as f64) as usize
}

/// Free device memory now, minus `reserve_gib`, in GiB (at least 0).
pub fn free_vram_gib(b: &impl Memory, reserve_gib: f64) -> Result<f64> {
    let (free, _) = b.mem_info()?;
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

/// Enqueues one record copy from a host slot to device address `dst` on `queue`.
fn enqueue_copy<B: Backend>(
    b: &B,
    queue: &B::CopyQueue,
    dst: u64,
    arena: &PinnedArena<B>,
    host_slot: usize,
) -> Result<()> {
    // SAFETY: dst is a slot of the VRAM arena that no kernel ordered before the copy
    // reads (KernelReadsValid); the source is a pinned host slot that is not
    // overwritten until this fetch's copy event completes (CopySourceValid).
    unsafe { b.copy_to_device(queue, dst, arena.slot_ptr(host_slot), arena.stride()) }
}

impl<B: Backend> ExpertSource<B> for TieredExperts<B> {
    fn fetch(&mut self, b: &B, layer: u32, experts: &[u32]) -> Result<Vec<u64>> {
        let staged = self.begin_fetch(b, layer, experts, false)?;
        self.finish_fetch(b)?;
        Ok(staged.addrs)
    }

    fn begin_fetch(&mut self, b: &B, layer: u32, experts: &[u32], host_ok: bool) -> Result<Staged> {
        self.finish_fetch(b)?;
        self.finish_lookahead(layer, experts)?;
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
        let mut host_budget = if host_ok && !streaming {
            self.host_compute
        } else {
            0
        };
        self.prune_host_seen();

        // VRAM placement: addresses now, fills (key, target, address) and
        // device-to-device promotions (source, destination) for later.
        let mut addrs = Vec::with_capacity(keys.len());
        let mut ready = Vec::with_capacity(keys.len());
        let mut host_rec = vec![None; keys.len()];
        let mut fills = Vec::new();
        let mut promotions = Vec::new();
        // Decode evictions saved into the stage (victim cache): (main slot, stage slot).
        let mut victims = Vec::new();
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
            if host_budget > 0
                && self.vram.peek(key).is_none()
                && self.stage.peek(key).is_none()
                && let Some(hs) = self.host.peek(key)
                && !self.admit(key)
            {
                host_budget -= 1;
                self.host.place(key, &pinned);
                self.stats.host_hits += 1;
                self.stats.host_computed += 1;
                host_rec[addrs.len()] = Some(self.host_arena.slot_ptr(hs) as usize);
                addrs.push(0);
                ready.push(false);
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
                Place::Miss(s, evicted) => {
                    let dst = self.main_addr(s);
                    // Keep the evicted record in VRAM: copy it into the stage first, so
                    // a later miss on it is a device-to-device promotion.
                    if let Some(old) = evicted
                        && self.stage.capacity() > 0
                        && let Some(Place::Miss(ss, _)) = self.stage.place(old, &pinned)
                    {
                        victims.push((dst, self.stage_addr(ss)));
                    }
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
            return Ok(Staged {
                addrs,
                ready,
                host: host_rec,
            });
        }

        // Refills wait for every kernel enqueued so far (KernelReadsValid).
        let after = self.b.record_compute()?;
        self.b.copies_wait(&self.copy_queue, &after)?;
        self.open = true;
        // Victim saves first: they read main slots that promotions and fills refill.
        for (src, dst) in victims.into_iter().chain(promotions) {
            // SAFETY: both are slots of the VRAM arena; the source is only refilled by
            // later copies on this same stream, and the destination is read only by
            // kernels ordered after finish_fetch.
            unsafe {
                self.b
                    .copy_on_device(&self.copy_queue, dst, src, self.stride)?
            };
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
        Ok(Staged {
            addrs,
            ready,
            host: host_rec,
        })
    }

    /// Reads predicted disk misses of `layer` into host slots (never into VRAM, and
    /// never evicting a slot an in-flight copy still reads), so its fetch finds them
    /// in the host tier.
    fn wants_prefetch(&self) -> bool {
        self.lookahead
    }

    fn prefetch(&mut self, b: &B, layer: u32, experts: &[u32]) -> Result<()> {
        self.finish_fetch(b)?;
        self.finish_lookahead(u32::MAX, &[])?;
        let keys: HashSet<u32> = experts
            .iter()
            .filter(|&&e| e < self.num_experts)
            .map(|&e| layer * self.num_experts + e)
            .collect();
        self.stats.predicted += keys.len() as u64;
        if self.lookahead {
            let mut jobs = Vec::new();
            for &key in &keys {
                if self.vram.peek(key).is_some()
                    || self.stage.peek(key).is_some()
                    || self.host.peek(key).is_some()
                {
                    continue;
                }
                let Some(Place::Miss(hs, _)) = self.host.place(key, &|_| false) else {
                    continue;
                };
                self.wait_copies(self.host_last_copy[hs])?;
                let (l, e) = (key / self.num_experts, key % self.num_experts);
                let (offset, stride) = self.model.record_location(l, e)?;
                jobs.push(ReadJob {
                    offset,
                    dst: self.host_arena.slot_ptr(hs),
                    len: stride as usize,
                    tag: jobs.len(),
                });
                self.lookahead_inflight.push(key);
            }
            self.stats.lookahead_reads += jobs.len() as u64;
            if !jobs.is_empty()
                && let Err(e) = self.reader.submit(jobs)
            {
                for k in self.lookahead_inflight.drain(..) {
                    self.host.forget(k);
                }
                return Err(e);
            }
        }
        self.prediction = Some((layer, keys));
        Ok(())
    }

    /// Retires main VRAM chunks from the top until `bytes` are freed, keeping at
    /// least one layer's worth of slots. KernelReadsValid: every copy and kernel that
    /// could read a retired slot has completed before its chunk is freed.
    fn release_vram(&mut self, b: &B, bytes: usize) -> Result<usize> {
        self.finish_fetch(b)?;
        let floor = self.num_experts as usize;
        let mut freed = 0;
        let mut synced = false;
        while freed < bytes && self.main_chunks.len() > 1 {
            let keep = (self.main_chunks.len() - 1) * CHUNK_SLOTS;
            if keep < floor {
                break;
            }
            if !synced {
                self.wait_copies(u64::MAX)?;
                self.b.copies_sync(&self.copy_queue)?;
                self.b.sync()?;
                synced = true;
            }
            self.vram.shrink(keep);
            self.main_chunks.pop();
            self.main_bases.pop();
            freed += CHUNK_SLOTS * self.stride;
        }
        Ok(freed)
    }

    /// Appends empty main chunks while `bytes` allow, up to the configured size.
    fn reclaim_vram(&mut self, _b: &B, bytes: usize) -> Result<usize> {
        let chunk_bytes = CHUNK_SLOTS * self.stride;
        let mut taken = 0;
        while taken + chunk_bytes <= bytes && self.main_chunks.len() < self.max_chunks {
            let Ok(chunk) = self.b.zeros_bytes(chunk_bytes) else {
                break;
            };
            self.main_bases.push(self.b.bytes_addr(&chunk));
            self.main_chunks.push(chunk);
            self.vram.grow(self.main_chunks.len() * CHUNK_SLOTS);
            taken += chunk_bytes;
        }
        Ok(taken)
    }

    fn finish_fetch(&mut self, _b: &B) -> Result<()> {
        if !self.open {
            return Ok(());
        }
        self.open = false;
        let reads = std::mem::take(&mut self.reads);
        let seq = self.seq;
        let (b, queue, host, host_last, stage, stage_last) = (
            &*self.b,
            &self.copy_queue,
            &self.host_arena,
            &mut self.host_last_copy,
            &self.host_stage,
            &mut self.host_stage_last_copy,
        );
        let drained = self.reader.drain(|tag| {
            let (src, dst, _, _) = reads[tag];
            match src {
                Src::Host(hs) => {
                    enqueue_copy(b, queue, dst, host, hs)?;
                    host_last[hs] = seq;
                }
                Src::Stage(ss) => {
                    enqueue_copy(b, queue, dst, stage, ss)?;
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
        let done = self.b.record_copies(&self.copy_queue)?;
        self.b.compute_wait(&done)?;
        self.pending.push_back((self.seq, done));
        // Drop completed events so the queue stays short.
        while self.pending.len() > 1 && self.b.event_done(&self.pending[0].1)? {
            self.pending.pop_front();
        }
        Ok(())
    }

    fn stats(&self) -> ExpertStats {
        self.stats
    }

    fn describe(&self) -> String {
        self.summary()
    }
}
