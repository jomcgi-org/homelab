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
//! **Stage-ahead** ([`ExpertSource::stage_ahead`], prefill): while one layer
//! computes, the model predicts the next layer's experts and the tier places the
//! predicted misses in free main slots, then the stage, then main slots by policy,
//! never evicting a record of the last fetched layer, and starts their copies and
//! disk reads. While the stage holds the computing layer, the next one borrows the
//! main tier's coldest records instead of a permanently larger stage, which would
//! cost decode hits on every step. The copies wait for
//! the compute-stream event like every refill (KernelReadsValid); the next fetch
//! makes the compute stream wait for them before it hands out any address, so a
//! staged record counts as resident from then on.
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
use oominf_core::{ExpertSource, ExpertStats, ExpertTiers, Memory, Staged, Transfer};
use oominf_format::Model;

use crate::cache::{Place, SlotCache};
use crate::host::{DirectReader, IoConfig, PinnedArena, ReadJob};
use crate::policy::Policy;

/// Where a pending copy reads from.
#[derive(Clone, Copy)]
enum Src {
    /// A host-tier slot.
    Host(usize),
    /// A host stage ring slot.
    Stage(usize),
}

/// A pending disk read: host buffer, device address, key, and the VRAM cache the
/// key was placed in.
type Read = (Src, u64, u32, Target);

/// Which reads: a stage-ahead's (on `reader`) or a fetch's own (on `fetch_reader`).
#[derive(Clone, Copy, PartialEq, Eq)]
enum Batch {
    Ahead,
    Fetch,
}

/// Which VRAM cache a pending fill belongs to.
#[derive(Clone, Copy)]
enum Target {
    Main,
    Stage,
}

const GIB: f64 = (1u64 << 30) as f64;

/// Reads in flight on the stage-ahead reader and on a fetch's reader. A single
/// record read is many block requests and keeps a drive near full speed.
const AHEAD_READ_DEPTH: u32 = 4;

/// A stage-ahead's host-tier copies go to the copy queue this many at a time (about
/// 2 ms of copy engine), the next only once those completed, as the caller polls
/// ([`ExpertSource::finish_stage_ahead`], which prefill calls while it waits for
/// routing). The copy engine runs ready copies in submission order across queues,
/// so a fetch's own copies (needed now) wait behind at most one trickle of the next
/// layer's, not all of it (measured: ~45 ms of idle device per layer with one batch,
/// ~13 ms with a quarter layer per fetch group).
const STAGE_TRICKLE: usize = 8;
const FETCH_READ_DEPTH: u32 = 32;

/// The copy that last read a host buffer: a fetch's (by fetch sequence number, on
/// the fetch copy queue) or a stage-ahead's (by batch, on the stage-ahead queue).
#[derive(Clone, Copy, Default)]
struct LastCopy {
    ahead: bool,
    seq: u64,
}

/// Records read per batch when preloading the host tier.
const PRELOAD_BATCH: usize = 64;

/// Records per chunk of the main VRAM arena (about 177 MB for Qwen 3.8 Flash).
pub const CHUNK_SLOTS: usize = 64;

/// Fetches within which a second host-tier miss of a key admits it to VRAM (about
/// 32 decode tokens of a 48-layer model).
const ADMIT_WINDOW: u64 = 48 * 32;

/// Requested tier sizes of one [`TieredExperts`], in records.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TierSizes {
    /// VRAM slots, the stage included (see [`TieredExperts::new`]).
    pub vram_slots: usize,
    /// Pinned host tier slots.
    pub host_slots: usize,
    /// Pinned host staging ring slots for prefill reads that find no free host
    /// slot (capped at one layer's experts).
    pub host_stage_slots: usize,
    /// Most distinct experts one fetch routes: a layer's experts for the decoder
    /// layers (a prefill routes most of them), fewer for a group only decode-sized
    /// steps fetch (e.g. a draft head's).
    pub max_fetch: usize,
}

/// Fetches with more experts than this (out of `num_experts` per layer) stream
/// through the stage.
pub fn stream_threshold(num_experts: usize) -> usize {
    (num_experts / 8).max(1)
}

/// Most distinct experts one fetch of a tier places without streaming through the
/// stage: `max_fetch` (see [`TierSizes`]), or `stream_threshold` once there is a
/// stage (larger fetches stream).
fn unstreamed(num_experts: usize, max_fetch: usize, stage: bool) -> usize {
    if stage {
        stream_threshold(num_experts).min(max_fetch)
    } else {
        max_fetch
    }
}

/// Fewest VRAM slots a tier works with: whole chunks holding the largest fetch
/// that does not stream (a fetch places every expert it routes).
pub fn min_vram_slots(num_experts: usize, max_fetch: usize, stage: bool) -> usize {
    unstreamed(num_experts, max_fetch, stage)
        .div_ceil(CHUNK_SLOTS)
        .max(1)
        * CHUNK_SLOTS
}

/// Fewest host slots a tier works with: the records of the largest fetch that does
/// not stream, each placed in the host tier on its way to VRAM. Lookahead reads use
/// only slots beyond these.
pub fn min_host_slots(num_experts: usize, max_fetch: usize, stage: bool) -> usize {
    unstreamed(num_experts, max_fetch, stage)
}

pub struct TieredExperts<B: Transfer + 'static> {
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
    // The readers come before the host buffers they fill: fields drop in order, and a
    // reader joins its worker (finishing any read in flight) when dropped.
    /// Reads for stage-ahead and preloading.
    reader: DirectReader,
    /// A fetch's own reads, on a separate ring: a prefill fetch that misses on disk
    /// waits for its few records, not for the next layer's whole stage-ahead batch.
    fetch_reader: DirectReader,
    /// Lookahead reads (decode), tagged by key: a fetch waits only for those of its
    /// own experts; the rest land in the background.
    lookahead_reader: DirectReader,
    host: SlotCache,
    host_arena: PinnedArena<B>,
    /// The last device copy that read each host slot.
    host_last_copy: Vec<LastCopy>,
    host_stage: PinnedArena<B>,
    host_stage_last_copy: Vec<LastCopy>,
    /// Host stage ring slots a disk read is still filling, and that read's batch.
    host_stage_reading: Vec<Option<Batch>>,
    host_stage_next: usize,
    /// Fetches with more experts than this stream through the stage.
    stream_threshold: usize,
    /// Most lookahead reads in flight (each pins its host slot until it lands).
    lookahead_cap: usize,
    /// Copy-stream events of fetches whose copies may still be pending, oldest first.
    pending: VecDeque<(u64, B::Event)>,
    copy_queue: B::CopyQueue,
    /// Stage-ahead copies run on their own queue: a fetch's compute waits for its
    /// own copies, not for the next layer's (which the next layer's first fetch
    /// waits for).
    ahead_queue: B::CopyQueue,
    /// Events after each finished stage-ahead batch's copies, oldest first, and the
    /// number of the open (or last) batch.
    ahead_pending: VecDeque<(u64, B::Event)>,
    ahead_batch: u64,
    /// Host-tier copies of the open stage-ahead not yet handed to the copy queue
    /// (host slot, device address), fed in slices (see `STAGE_SLICES`).
    ahead_unsent: VecDeque<(usize, u64)>,
    /// Copy-queue event after the last trickle sent.
    ahead_trickle: Option<B::Event>,
    /// Disk reads of the open stage-ahead and of the open fetch, per read tag: (host
    /// buffer, device address, key, VRAM cache the key was placed in).
    ahead_reads: Vec<Read>,
    fetch_reads: Vec<Read>,
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
    /// started.
    prediction: Option<(u32, HashSet<u32>)>,
    lookahead_issued: Vec<u32>,
    /// Keys whose lookahead reads have not landed yet: pinned in the host tier, and
    /// waited for by a fetch that routes them.
    lookahead_inflight: HashSet<u32>,
    /// Stage-ahead copies or reads were queued that `finish_stage_ahead` has not
    /// completed yet.
    ahead_open: bool,
    /// Copy-stream event after the last stage-ahead's copies, for the next fetch's
    /// compute to wait on.
    ahead_done: Option<B::Event>,
    /// The last stage-ahead's layer and the keys it loaded.
    ahead_keys: Option<(u32, HashSet<u32>)>,
    /// The layer of the last fetch (its records are pinned during a stage-ahead).
    last_layer: Option<u32>,
    /// Compute event at that layer's first fetch: every kernel reading another
    /// layer's records was queued before it.
    layer_start: Option<B::Event>,
    seq: u64,
    pub stats: ExpertStats,
}

impl<B: Transfer + 'static> TieredExperts<B> {
    /// Serves the expert groups whose records have layout `layout` (all records of
    /// one layout share a size), with tiers of `sizes` records (see [`slots_for`])
    /// read as `io` says.
    ///
    /// Degrades rather than fails when memory runs short at allocation time: VRAM
    /// chunks are allocated until one fails (the tier keeps those, down to
    /// [`min_vram_slots`]), the stage is dropped if it cannot be allocated, and the
    /// pinned arenas retry smaller down to [`min_host_slots`]. Each shortfall is
    /// logged.
    pub fn new(
        b: Arc<B>,
        model: Arc<Model>,
        layout: &str,
        sizes: TierSizes,
        vram_policy: Box<dyn Policy>,
        host_policy: Box<dyn Policy>,
        io: &IoConfig,
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
        let TierSizes {
            vram_slots,
            host_slots,
            host_stage_slots,
            max_fetch,
        } = sizes;
        ensure!(
            vram_slots > 0 && host_slots > 0,
            "tier sizes must be positive"
        );
        // Set one layer's worth of slots aside for streaming when the budget allows.
        let ne = num_experts as usize;
        let mut stage_slots = if vram_slots >= 2 * ne { ne } else { 0 };
        let min_vram = min_vram_slots(ne, max_fetch, stage_slots > 0);
        let mut want_chunks = (vram_slots - stage_slots) / CHUNK_SLOTS;
        ensure!(
            want_chunks * CHUNK_SLOTS >= min_vram,
            "VRAM tier of {vram_slots} {layout} records ({:.2} GiB) is below the minimum of {min_vram} ({:.2} GiB: its largest unstreamed fetch in {CHUNK_SLOTS}-record chunks)",
            (vram_slots * stride) as f64 / GIB,
            (min_vram * stride) as f64 / GIB
        );
        let mut stage_arena = match stage_slots {
            0 => None,
            n => match b.zeros_bytes(n * stride) {
                Ok(a) => Some(a),
                Err(e) => {
                    eprintln!(
                        "oominf: warning: no VRAM stage for {layout} experts ({e:#}); prefill fetches go through the main tier"
                    );
                    stage_slots = 0;
                    None
                }
            },
        };
        let mut main_chunks = Vec::with_capacity(want_chunks);
        while main_chunks.len() < want_chunks {
            let e = match b.zeros_bytes(CHUNK_SLOTS * stride) {
                Ok(c) => {
                    main_chunks.push(c);
                    continue;
                }
                Err(e) => e,
            };
            let min_vram = min_vram_slots(ne, max_fetch, stage_slots > 0);
            if main_chunks.len() * CHUNK_SLOTS >= min_vram {
                eprintln!(
                    "oominf: warning: VRAM tier for {layout} experts stopped at {} of {} records ({e:#})",
                    main_chunks.len() * CHUNK_SLOTS,
                    want_chunks * CHUNK_SLOTS
                );
                break;
            }
            if stage_arena.take().is_some() {
                // The main tier's minimum matters more than streaming prefill.
                eprintln!(
                    "oominf: warning: dropped the VRAM stage for {layout} experts to fit the main tier's minimum ({e:#})"
                );
                stage_slots = 0;
                want_chunks = vram_slots / CHUNK_SLOTS;
                continue;
            }
            return Err(e.context(format!(
                "allocating the minimum VRAM tier of {min_vram} {layout} records ({:.2} GiB)",
                (min_vram * stride) as f64 / GIB
            )));
        }
        let stage_base = stage_arena.as_ref().map_or(0, |a| b.bytes_addr(a));
        let min_vram = min_vram_slots(ne, max_fetch, stage_slots > 0);
        ensure!(
            main_chunks.len() * CHUNK_SLOTS >= min_vram,
            "VRAM tier of {} {layout} records is below the minimum of {min_vram} without a stage",
            main_chunks.len() * CHUNK_SLOTS
        );
        let max_chunks = main_chunks.len();
        let main_slots = max_chunks * CHUNK_SLOTS;
        let main_bases = main_chunks.iter().map(|c| b.bytes_addr(c)).collect();
        let min_host = min_host_slots(ne, max_fetch, stage_slots > 0);
        ensure!(
            host_slots >= min_host,
            "host tier of {host_slots} {layout} records ({:.2} GiB) is below the minimum of {min_host} ({:.2} GiB)",
            (host_slots * stride) as f64 / GIB,
            (min_host * stride) as f64 / GIB
        );
        let (host_arena, short) =
            PinnedArena::new_at_most(b.clone(), host_slots, min_host, stride)?;
        if let Some(s) = short {
            eprintln!("oominf: warning: host tier for {layout} experts: {s}");
        }
        let host_slots = host_arena.slots();
        // A fetch or a stage-ahead reads at most one layer's worth of records through
        // the ring; a smaller ring makes them wait for its slots to come round.
        let ring = host_stage_slots.clamp(1, stage_slots.max(1));
        let (host_stage, short) = PinnedArena::new_at_most(b.clone(), ring, 1, stride)?;
        if let Some(s) = short {
            eprintln!("oominf: warning: host staging ring for {layout} experts: {s}");
        }
        let ring = host_stage.slots();
        let experts_file = model.dir().join(oominf_format::EXPERTS_FILE);
        // Stage-ahead reads keep a shallow queue so a fetch's own reads, which the
        // device waits for, do not queue behind a whole layer of them.
        let reader = DirectReader::open_with(&experts_file, AHEAD_READ_DEPTH, io)?;
        let fetch_reader = DirectReader::open_with(&experts_file, FETCH_READ_DEPTH, io)?;
        let lookahead_reader = DirectReader::open_with(&experts_file, AHEAD_READ_DEPTH, io)?;
        let layers = groups.iter().map(|g| g.layer).collect();
        let stream_threshold = stream_threshold(ne);
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
            host_last_copy: vec![LastCopy::default(); host_slots],
            host_arena,
            host_stage_last_copy: vec![LastCopy::default(); ring],
            host_stage_reading: vec![None; ring],
            host_stage,
            host_stage_next: 0,
            stream_threshold,
            // Lookahead never pins so many host slots that a fetch's own records
            // could not be placed.
            lookahead_cap: host_slots.saturating_sub(min_host),
            pending: VecDeque::new(),
            copy_queue: b.copy_queue()?,
            ahead_queue: b.copy_queue()?,
            ahead_pending: VecDeque::new(),
            ahead_batch: 0,
            ahead_unsent: VecDeque::new(),
            ahead_trickle: None,
            reader,
            fetch_reader,
            lookahead_reader,
            ahead_reads: Vec::new(),
            fetch_reads: Vec::new(),
            open: false,
            lookahead: false,
            host_compute: 0,
            host_seen: HashMap::new(),
            prediction: None,
            lookahead_issued: Vec::new(),
            lookahead_inflight: HashSet::new(),
            ahead_open: false,
            ahead_done: None,
            ahead_keys: None,
            layer_start: None,
            last_layer: None,
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
        let both = self
            .vram
            .keys()
            .chain(self.stage.keys())
            .filter(|&k| self.host.peek(k).is_some())
            .count();
        let held = self.vram.len() + self.stage.len() + self.host.len() - both;
        format!(
            "{}; held: VRAM {} + stage {} + host {} records, {} in both VRAM and host, {} distinct; disk reads so far {}",
            self.summary_sizes(),
            self.vram.len(),
            self.stage.len(),
            self.host.len(),
            both,
            held,
            self.stats.disk_reads
        )
    }

    fn summary_sizes(&self) -> String {
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

    /// Waits until the copy `last` has completed (a host buffer it read can then be
    /// overwritten).
    fn wait_copies(&mut self, last: LastCopy) -> Result<()> {
        let pending = if last.ahead {
            if self.ahead_open && last.seq == self.ahead_batch {
                // The open stage-ahead's copy: send the ones it holds back and wait
                // for its queue so far, leaving the batch open (its later reads
                // still land through it).
                self.send_ahead_copies(usize::MAX)?;
                let ev = self.b.record_copies(&self.ahead_queue)?;
                return self.b.event_wait(&ev);
            }
            &mut self.ahead_pending
        } else {
            if self.open && last.seq == self.seq {
                // A copy of the fetch still open (its event comes at
                // `finish_fetch`): wait for its queue so far.
                let ev = self.b.record_copies(&self.copy_queue)?;
                return self.b.event_wait(&ev);
            }
            &mut self.pending
        };
        while let Some((s, _)) = pending.front() {
            if *s > last.seq {
                break;
            }
            let (_, ev) = pending.pop_front().unwrap();
            self.b.event_wait(&ev)?;
        }
        Ok(())
    }

    /// Enqueues the copy of host slot `host_slot` to device address `dst` on the
    /// copy stream.
    fn copy_host(&mut self, host_slot: usize, dst: u64) -> Result<()> {
        enqueue_copy(&*self.b, &self.copy_queue, dst, &self.host_arena, host_slot)?;
        self.host_last_copy[host_slot] = LastCopy {
            ahead: false,
            seq: self.seq,
        };
        Ok(())
    }

    /// Next host stage ring slot for a read of `batch`, once the read filling it
    /// has landed and the copy that last read it has completed. `jobs` are the
    /// caller's reads of `batch` not yet submitted: when the ring has come round to
    /// a slot one of them fills, they are submitted first, so waiting for that read
    /// cannot lose or double-book it.
    fn next_host_stage(&mut self, batch: Batch, jobs: &mut Vec<ReadJob>) -> Result<usize> {
        let slot = self.host_stage_next;
        self.host_stage_next = (slot + 1) % self.host_stage_last_copy.len();
        if let Some(by) = self.host_stage_reading[slot] {
            if by == batch && !jobs.is_empty() {
                self.submit(batch, std::mem::take(jobs))?;
            }
            // The batch stays open: its later reads land through it as before.
            self.land_submitted(by)?;
        }
        self.wait_copies(self.host_stage_last_copy[slot])?;
        Ok(slot)
    }

    /// Submits reads of `batch`; on failure abandons the batch.
    fn submit(&mut self, batch: Batch, jobs: Vec<ReadJob>) -> Result<()> {
        if jobs.is_empty() {
            return Ok(());
        }
        let reader = match batch {
            Batch::Ahead => &mut self.reader,
            Batch::Fetch => &mut self.fetch_reader,
        };
        if let Err(e) = reader.submit(jobs) {
            self.abandon(batch);
            return Err(e);
        }
        Ok(())
    }

    /// Queues a disk read of `key` into `src` that will then be copied to `dst`.
    fn queue_read(
        &mut self,
        batch: Batch,
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
            Src::Stage(ss) => {
                self.host_stage_reading[ss] = Some(batch);
                self.host_stage.slot_ptr(ss)
            }
        };
        let reads = match batch {
            Batch::Ahead => &mut self.ahead_reads,
            Batch::Fetch => &mut self.fetch_reads,
        };
        jobs.push(ReadJob {
            offset,
            dst: buf,
            len: stride as usize,
            tag: reads.len(),
        });
        reads.push((src, dst, key, target));
        Ok(())
    }

    /// Waits for the lookahead reads of `layer`'s routed `experts` (their records are
    /// host-resident from then on; other lookahead reads keep landing in the
    /// background), or with `layer == u32::MAX` for every lookahead read, and scores
    /// the last prediction against the routing.
    fn finish_lookahead(&mut self, layer: u32, experts: &[u32]) -> Result<()> {
        let inflight = &mut self.lookahead_inflight;
        let landed = |tag: usize| {
            inflight.remove(&(tag as u32));
            Ok(())
        };
        let waited = if layer == u32::MAX {
            self.lookahead_reader.drain(landed)
        } else {
            let need: HashSet<usize> = experts
                .iter()
                .map(|&e| (layer * self.num_experts + e) as usize)
                .filter(|k| self.lookahead_inflight.contains(&(*k as u32)))
                .collect();
            let inflight = &mut self.lookahead_inflight;
            self.lookahead_reader.wait_for(&need, |tag| {
                inflight.remove(&(tag as u32));
                Ok(())
            })
        };
        if let Err(e) = waited {
            // Nothing is reused while reads are in flight: finish them all first.
            let _ = self.lookahead_reader.drain(|_| Ok(()));
            for k in self.lookahead_inflight.drain() {
                self.host.forget(k);
            }
            return Err(e);
        }
        if let Some((pl, keys)) = self.prediction.take()
            && pl == layer
        {
            let routed: HashSet<u32> = experts
                .iter()
                .map(|&e| layer * self.num_experts + e)
                .collect();
            self.stats.routed_after_prediction += routed.len() as u64;
            self.stats.predicted_routed += keys.intersection(&routed).count() as u64;
            self.stats.lookahead_used += self
                .lookahead_issued
                .iter()
                .filter(|k| routed.contains(k))
                .count() as u64;
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

    /// Waits for a batch of disk reads, queueing each record's device copy as its
    /// read lands, and closes the batch's read list.
    fn land_reads(&mut self, batch: Batch) -> Result<()> {
        self.land_submitted(batch)?;
        match batch {
            Batch::Ahead => self.ahead_reads.clear(),
            Batch::Fetch => self.fetch_reads.clear(),
        }
        Ok(())
    }

    /// Waits for every submitted read of `batch`, queueing each record's device
    /// copy as its read lands; reads queued later still land through the batch. On
    /// an error the batch is abandoned.
    fn land_submitted(&mut self, batch: Batch) -> Result<()> {
        let (reads, reader, queue, copy) = match batch {
            Batch::Ahead => (
                &self.ahead_reads,
                &mut self.reader,
                &self.ahead_queue,
                LastCopy {
                    ahead: true,
                    seq: self.ahead_batch,
                },
            ),
            Batch::Fetch => (
                &self.fetch_reads,
                &mut self.fetch_reader,
                &self.copy_queue,
                LastCopy {
                    ahead: false,
                    seq: self.seq,
                },
            ),
        };
        let mut land = Lander {
            b: &*self.b,
            queue,
            host: &self.host_arena,
            host_last: &mut self.host_last_copy,
            stage: &self.host_stage,
            stage_last: &mut self.host_stage_last_copy,
            stage_reading: &mut self.host_stage_reading,
            copy,
        };
        if let Err(e) = reader.drain(|tag| land.land(reads[tag])) {
            self.abandon(batch);
            return Err(e);
        }
        Ok(())
    }

    /// Gives up on `batch` after an error: once every read it submitted has finished
    /// (none can still write a buffer) and every copy queued so far has completed
    /// (none still reads a host buffer or writes a VRAM slot), its records are
    /// forgotten and their slots freed for reuse.
    fn abandon(&mut self, batch: Batch) {
        let reader = match batch {
            Batch::Ahead => &mut self.reader,
            Batch::Fetch => &mut self.fetch_reader,
        };
        let _ = reader.drain(|_| Ok(()));
        let _ = self.b.copies_sync(&self.copy_queue);
        let _ = self.b.copies_sync(&self.ahead_queue);
        self.forget_reads(batch);
    }

    /// Queues the device copies of the open stage-ahead's reads that have landed,
    /// without waiting for the rest.
    /// Places `experts` of `layer` (a prediction) for the open stage-ahead batch
    /// and starts loading those not yet in VRAM: host-tier records copy in slices,
    /// the rest read from disk first. Records already staged are skipped.
    fn stage_keys(&mut self, layer: u32, experts: &[u32]) -> Result<()> {
        let ne = self.num_experts;
        let mut keys: Vec<u32> = experts
            .iter()
            .filter(|&&e| e < ne)
            .map(|&e| layer * ne + e)
            .collect();
        keys.sort_unstable();
        keys.dedup();
        let (Some((_, staged)), Some((_, predicted))) =
            (&mut self.ahead_keys, &mut self.prediction)
        else {
            return Ok(());
        };
        keys.retain(|k| !predicted.contains(k));
        self.stats.predicted += keys.len() as u64;
        predicted.extend(keys.iter().copied());
        let predicted = predicted.clone();
        let mut staged_now = std::mem::take(staged);
        let current = self.last_layer;
        let pinned = |k: u32| predicted.contains(&k) || Some(k / ne) == current;

        // Main slots (free ones first, then by policy), then the stage: the stage
        // belongs to the computing layer, whose later fetch groups stream their
        // misses through it and would evict staged records there; the next layer
        // borrows the main tier's coldest records instead (once-staged records score
        // lowest, so later layers evict each other before any decode-hot record).
        let mut fills = Vec::new();
        for &key in &keys {
            if self.vram.peek(key).is_some() || self.stage.peek(key).is_some() {
                continue;
            }
            if let Some(Place::Miss(s, _)) = self.vram.place(key, &pinned) {
                fills.push((key, Target::Main, self.main_addr(s)));
                continue;
            }
            match self.stage.place(key, &pinned) {
                Some(Place::Miss(s, _)) => fills.push((key, Target::Stage, self.stage_addr(s))),
                // Every slot of both holds a pinned record.
                _ => break,
            }
        }
        self.stats.staged += fills.len() as u64;
        staged_now.extend(fills.iter().map(|&(k, _, _)| k));
        self.ahead_keys = Some((layer, staged_now));
        let copy = LastCopy {
            ahead: true,
            seq: self.ahead_batch,
        };
        let mut jobs = Vec::new();
        for (i, &(key, target, dst)) in fills.iter().enumerate() {
            let loaded = match self.host.peek(key) {
                // Sent in slices; until sent, the slot counts as read by this batch
                // (CopySourceValid: finish_staging sends the rest before anything
                // may overwrite it).
                Some(hs) => {
                    self.ahead_unsent.push_back((hs, dst));
                    self.host_last_copy[hs] = copy;
                    Ok(())
                }
                None if self.host.has_free() => {
                    let Some(Place::Miss(hs, None)) = self.host.place(key, &pinned) else {
                        unreachable!("a free slot evicts nothing");
                    };
                    self.queue_read(Batch::Ahead, &mut jobs, key, Src::Host(hs), dst, target)
                }
                None => self
                    .next_host_stage(Batch::Ahead, &mut jobs)
                    .and_then(|ss| {
                        self.queue_read(Batch::Ahead, &mut jobs, key, Src::Stage(ss), dst, target)
                    }),
            };
            if let Err(e) = loaded {
                self.abandon_fills(Batch::Ahead, &fills[i..]);
                return Err(e);
            }
        }
        self.trickle()?;
        self.submit(Batch::Ahead, jobs)
    }

    /// After an error part-way through loading `fills` (records placed in VRAM),
    /// abandons `batch` and forgets the records not loaded yet, so no slot claims a
    /// record it does not hold.
    fn abandon_fills(&mut self, batch: Batch, rest: &[(u32, Target, u64)]) {
        self.abandon(batch);
        if let Some(&(key, _, _)) = rest.first() {
            // Its host slot may have been placed without its read.
            self.host.forget(key);
        }
        for &(key, target, _) in rest {
            match target {
                Target::Main => self.vram.forget(key),
                Target::Stage => self.stage.forget(key),
            }
        }
    }

    /// Loads one of a fetch's `fills` (placed in VRAM): a host hit copies now, the
    /// rest read from disk into a host slot or (streaming) a ring slot.
    #[allow(clippy::too_many_arguments)]
    fn fill_fetch(
        &mut self,
        key: u32,
        target: Target,
        dst: u64,
        streaming: bool,
        pinned: &dyn Fn(u32) -> bool,
        jobs: &mut Vec<ReadJob>,
    ) -> Result<()> {
        if streaming {
            match self.host.peek(key) {
                Some(hs) => {
                    self.stats.host_hits += 1;
                    self.copy_host(hs, dst)?;
                }
                // The stage-ahead's reads stay in flight: a free host slot is
                // nobody's read target, and a ring slot is handed out only once its
                // read has landed.
                None if self.host.has_free() => {
                    let Some(Place::Miss(hs, None)) = self.host.place(key, pinned) else {
                        unreachable!("a free slot evicts nothing");
                    };
                    self.queue_read(Batch::Fetch, jobs, key, Src::Host(hs), dst, target)?;
                }
                None => {
                    let ss = self.next_host_stage(Batch::Fetch, jobs)?;
                    self.queue_read(Batch::Fetch, jobs, key, Src::Stage(ss), dst, target)?;
                }
            }
            return Ok(());
        }
        let reading = &self.lookahead_inflight;
        let place = match self.host.place(key, &|k| pinned(k) || reading.contains(&k)) {
            Some(p) => p,
            None => {
                // Lookahead reads in flight pin every other slot: let them land
                // (their records then compete like any other) and place again.
                self.finish_lookahead(u32::MAX, &[])?;
                self.host
                    .place(key, pinned)
                    .context("host expert tier is smaller than one layer's routed experts")?
            }
        };
        match place {
            Place::Hit(hs) => {
                self.stats.host_hits += 1;
                self.copy_host(hs, dst)?;
            }
            Place::Miss(hs, _) => {
                // An eviction may hit a slot a stage-ahead read still fills.
                self.finish_staging()?;
                self.wait_copies(self.host_last_copy[hs])?;
                self.queue_read(Batch::Fetch, jobs, key, Src::Host(hs), dst, target)?;
            }
        }
        Ok(())
    }

    /// Hands up to `n` of the open stage-ahead's host-tier copies to its queue.
    fn send_ahead_copies(&mut self, n: usize) -> Result<()> {
        let copy = LastCopy {
            ahead: true,
            seq: self.ahead_batch,
        };
        for _ in 0..n {
            let Some((hs, dst)) = self.ahead_unsent.pop_front() else {
                break;
            };
            enqueue_copy(&*self.b, &self.ahead_queue, dst, &self.host_arena, hs)?;
            self.host_last_copy[hs] = copy;
        }
        Ok(())
    }

    /// Sends the next trickle of the open stage-ahead's host-tier copies once the
    /// last one completed.
    fn trickle(&mut self) -> Result<()> {
        if self.ahead_unsent.is_empty() {
            return Ok(());
        }
        if let Some(ev) = &self.ahead_trickle
            && !self.b.event_done(ev)?
        {
            return Ok(());
        }
        self.send_ahead_copies(STAGE_TRICKLE)?;
        self.ahead_trickle = Some(self.b.record_copies(&self.ahead_queue)?);
        Ok(())
    }

    fn poll_staging(&mut self) -> Result<()> {
        if !self.ahead_open {
            return Ok(());
        }
        self.trickle()?;
        let mut land = Lander {
            b: &*self.b,
            queue: &self.ahead_queue,
            host: &self.host_arena,
            host_last: &mut self.host_last_copy,
            stage: &self.host_stage,
            stage_last: &mut self.host_stage_last_copy,
            stage_reading: &mut self.host_stage_reading,
            copy: LastCopy {
                ahead: true,
                seq: self.ahead_batch,
            },
        };
        let reads = &self.ahead_reads;
        self.reader.poll(|tag| land.land(reads[tag]))
    }

    /// Completes the open stage-ahead (its reads land and their copies are queued)
    /// and makes the compute stream wait for every stage-ahead copy, so staged
    /// records can be handed out as resident.
    fn wait_staged(&mut self) -> Result<()> {
        self.finish_staging()?;
        if let Some(ev) = self.ahead_done.take() {
            self.b.compute_wait(&ev)?;
        }
        Ok(())
    }

    fn finish_staging(&mut self) -> Result<()> {
        if !self.ahead_open {
            return Ok(());
        }
        self.ahead_open = false;
        self.send_ahead_copies(usize::MAX)?;
        self.ahead_trickle = None;
        self.land_reads(Batch::Ahead)?;
        let done = self.b.record_copies(&self.ahead_queue)?;
        self.ahead_pending.push_back((self.ahead_batch, done));
        while self.ahead_pending.len() > 1 && self.b.event_done(&self.ahead_pending[0].1)? {
            self.ahead_pending.pop_front();
        }
        self.ahead_done = Some(self.b.record_copies(&self.ahead_queue)?);
        Ok(())
    }

    fn forget_reads(&mut self, batch: Batch) {
        let reads = match batch {
            Batch::Ahead => std::mem::take(&mut self.ahead_reads),
            Batch::Fetch => std::mem::take(&mut self.fetch_reads),
        };
        for &(src, _, key, target) in &reads {
            match src {
                Src::Host(_) => self.host.forget(key),
                Src::Stage(ss) => self.host_stage_reading[ss] = None,
            }
            match target {
                Target::Main => self.vram.forget(key),
                Target::Stage => self.stage.forget(key),
            }
        }
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

/// Enqueues one record copy from a host slot to device address `dst` on `queue`.
fn enqueue_copy<B: Transfer>(
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

/// Queues a landed read's device copy and records which fetch's copy read its host
/// buffer (CopySourceValid); a ring slot is free for reuse once its read landed.
struct Lander<'a, B: Transfer> {
    b: &'a B,
    queue: &'a B::CopyQueue,
    host: &'a PinnedArena<B>,
    host_last: &'a mut [LastCopy],
    stage: &'a PinnedArena<B>,
    stage_last: &'a mut [LastCopy],
    stage_reading: &'a mut [Option<Batch>],
    copy: LastCopy,
}

impl<B: Transfer> Lander<'_, B> {
    fn land(&mut self, (src, dst, _, _): Read) -> Result<()> {
        match src {
            Src::Host(hs) => {
                enqueue_copy(self.b, self.queue, dst, self.host, hs)?;
                self.host_last[hs] = self.copy;
            }
            Src::Stage(ss) => {
                enqueue_copy(self.b, self.queue, dst, self.stage, ss)?;
                self.stage_last[ss] = self.copy;
                self.stage_reading[ss] = None;
            }
        }
        Ok(())
    }
}

impl<B: Transfer + 'static> ExpertSource<B> for TieredExperts<B> {
    fn fetch(&mut self, b: &B, layer: u32, experts: &[u32]) -> Result<Vec<u64>> {
        let staged = self.begin_fetch(b, layer, experts, false)?;
        self.finish_fetch(b)?;
        Ok(staged.addrs)
    }

    fn begin_fetch(&mut self, b: &B, layer: u32, experts: &[u32], host_ok: bool) -> Result<Staged> {
        self.finish_fetch(b)?;
        // The stage-ahead's records are handed out to its own layer only, so a fetch
        // for the layer computing while the next one stages neither waits for its
        // reads and copies nor evicts its records (in VRAM or the host tier). Should
        // this fetch read from disk itself, the stage's reads land first (they share
        // the reader and host staging buffers).
        let staging_next = matches!(&self.ahead_keys, Some((l, _)) if *l != layer);
        let ahead: HashSet<u32> = match &self.ahead_keys {
            Some((_, keys)) if staging_next => keys.clone(),
            _ => HashSet::new(),
        };
        if !staging_next {
            self.wait_staged()?;
        }
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
        // The stage-ahead belongs to its own layer: earlier fetch groups of the
        // computing layer leave it in place (and keep its records pinned), so its
        // reads overlap the whole layer instead of only the first groups.
        if !staging_next && let Some((_, staged)) = self.ahead_keys.take() {
            self.stats.staged_used += staged.intersection(&wanted).count() as u64;
        }
        if self.last_layer != Some(layer) {
            self.layer_start = Some(self.b.record_compute()?);
        }
        self.last_layer = Some(layer);
        let pinned = |k: u32| wanted.contains(&k) || ahead.contains(&k);
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
        for (i, &(key, target, dst)) in fills.iter().enumerate() {
            if let Err(e) = self.fill_fetch(key, target, dst, streaming, &pinned, &mut jobs) {
                self.abandon_fills(Batch::Fetch, &fills[i..]);
                return Err(e);
            }
        }
        self.submit(Batch::Fetch, jobs)?;
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
        self.finish_staging()?;
        // Earlier lookahead reads that have landed no longer need pinning.
        let inflight = &mut self.lookahead_inflight;
        self.lookahead_reader.poll(|tag| {
            inflight.remove(&(tag as u32));
            Ok(())
        })?;
        let keys: HashSet<u32> = experts
            .iter()
            .filter(|&&e| e < self.num_experts)
            .map(|&e| layer * self.num_experts + e)
            .collect();
        self.stats.predicted += keys.len() as u64;
        if self.lookahead {
            let mut jobs = Vec::new();
            self.lookahead_issued.clear();
            for &key in &keys {
                if self.vram.peek(key).is_some()
                    || self.stage.peek(key).is_some()
                    || self.host.peek(key).is_some()
                {
                    continue;
                }
                // Leave a fetch's own records room in a small host tier.
                if self.lookahead_inflight.len() >= self.lookahead_cap {
                    break;
                }
                let reading = &self.lookahead_inflight;
                let Some(Place::Miss(hs, _)) = self.host.place(key, &|k| reading.contains(&k))
                else {
                    continue;
                };
                self.wait_copies(self.host_last_copy[hs])?;
                let (l, e) = (key / self.num_experts, key % self.num_experts);
                let (offset, stride) = self.model.record_location(l, e)?;
                jobs.push(ReadJob {
                    offset,
                    dst: self.host_arena.slot_ptr(hs),
                    len: stride as usize,
                    tag: key as usize,
                });
                self.lookahead_inflight.insert(key);
                self.lookahead_issued.push(key);
            }
            self.stats.lookahead_reads += jobs.len() as u64;
            if !jobs.is_empty()
                && let Err(e) = self.lookahead_reader.submit(jobs)
            {
                for k in self.lookahead_issued.drain(..) {
                    self.lookahead_inflight.remove(&k);
                    self.host.forget(k);
                }
                return Err(e);
            }
        }
        self.prediction = Some((layer, keys));
        Ok(())
    }

    fn stages_ahead(&self) -> bool {
        self.stage.capacity() > 0
    }

    fn stage_ahead(&mut self, b: &B, layer: u32, experts: &[u32]) -> Result<()> {
        self.finish_fetch(b)?;
        self.finish_staging()?;
        self.finish_lookahead(u32::MAX, &[])?;
        self.seq += 1;
        self.ahead_keys = Some((layer, HashSet::new()));
        self.prediction = Some((layer, HashSet::new()));
        // Refills overwrite records of layers other than the computing one (pinned),
        // and every kernel reading those was queued before that layer's first fetch
        // (KernelReadsValid), so the copies overlap the computing layer's kernels.
        let after = match self.layer_start.take() {
            Some(ev) => ev,
            None => self.b.record_compute()?,
        };
        self.b.copies_wait(&self.ahead_queue, &after)?;
        // The copy engine runs ready copies in submission order across queues: the
        // computing layer's fetch copies (needed now) go first, or they wait behind
        // the next layer's (measured: ~50 ms of idle device per layer).
        let fetched = self.b.record_copies(&self.copy_queue)?;
        self.b.copies_wait(&self.ahead_queue, &fetched)?;
        self.ahead_open = true;
        self.ahead_batch += 1;
        self.stage_keys(layer, experts)
    }

    fn stage_more(&mut self, _b: &B, layer: u32, experts: &[u32]) -> Result<()> {
        // Only while that layer's batch is still open: once its first fetch began,
        // the stage-ahead is over and the fetches load the rest.
        if !self.ahead_open || !matches!(&self.ahead_keys, Some((l, _)) if *l == layer) {
            return Ok(());
        }
        self.stage_keys(layer, experts)
    }

    fn finish_stage_ahead(&mut self, _b: &B) -> Result<()> {
        self.poll_staging()
    }

    /// Retires main VRAM chunks from the top until `bytes` are freed, keeping at
    /// least one layer's worth of slots. KernelReadsValid: every copy and kernel that
    /// could read a retired slot has completed before its chunk is freed.
    fn release_vram(&mut self, b: &B, bytes: usize) -> Result<usize> {
        self.finish_fetch(b)?;
        self.finish_staging()?;
        let floor = self.num_experts as usize;
        let mut freed = 0;
        let mut synced = false;
        while freed < bytes && self.main_chunks.len() > 1 {
            let keep = (self.main_chunks.len() - 1) * CHUNK_SLOTS;
            if keep < floor {
                break;
            }
            if !synced {
                self.b.copies_sync(&self.copy_queue)?;
                self.b.copies_sync(&self.ahead_queue)?;
                self.pending.clear();
                self.ahead_pending.clear();
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

    fn releasable_vram(&self) -> usize {
        // `release_vram` keeps at least one chunk and `num_experts` slots.
        let min = (self.num_experts as usize).div_ceil(CHUNK_SLOTS).max(1);
        self.main_chunks.len().saturating_sub(min) * CHUNK_SLOTS * self.stride
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
        // A stage-ahead's reads still in flight are not this fetch's to wait for.
        if !self.fetch_reads.is_empty() {
            self.land_reads(Batch::Fetch)?;
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

    fn tiers(&self) -> ExpertTiers {
        let records = (self.layers.len() * self.num_experts as usize) as u64;
        let vram = (self.vram.capacity() + self.stage.capacity()) as u64;
        let host = self.host.capacity() as u64;
        let stride = self.stride as u64;
        ExpertTiers {
            records,
            bytes: records * stride,
            vram_records: vram,
            vram_bytes: vram * stride,
            host_records: host,
            host_bytes: host * stride,
        }
    }

    fn describe(&self) -> String {
        self.summary()
    }
}

#[cfg(test)]
#[path = "tiered_tests.rs"]
mod tests;
