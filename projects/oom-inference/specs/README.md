# Protocol specs

TLA+ specs for the oom-inference protocols where the subtle bugs live:
caching, tiering and asynchronous reuse of memory. Numerical correctness is
covered elsewhere (reference fixtures and the parity gate); these specs cover
"who may touch which buffer when".

| Spec | Status |
|---|---|
| `ExpertTiering.tla` | Safety and liveness checked |
| Prefix cache | Planned |
| KV / recurrent-state allocation and scheduler | Planned |

## Running

```
specs/run.sh          # every config
specs/run.sh ci       # the fast configs (CI): small, small_indirect, live_indirect
specs/run.sh large    # one config by name
specs/run.sh bugs     # each buggy variant; every one must be caught
```

Needs Java 17+ and `tla2tools.jar` in `$TLA_HOME` (default
`/disks/nvme-02/src/.toolchains/tla`, from
https://github.com/tlaplus/tlaplus/releases). TLC runs nice'd on cores
`$TLC_CPUS` (default 12-15) with 4 workers. `MC.tla` wraps the spec to add
symmetry for the safety-only configs (host buffers are permuted only within
the cache or within staging; symmetry is unsound for liveness, so the
liveness configs do not use it).

## ExpertTiering

### What is modelled

- **Tiers.** Every expert is always on disk. A fixed arena of host buffers and
  an arena of VRAM slots cache copies. Each buffer or slot is `free`,
  `loading` or `ready`.
- **Giving VRAM back.** The VRAM tier retires slots (`Retire`) when other
  device memory such as a growing KV cache needs room, and restores them
  (`Restore`) when it is free again, keeping at least `MinSlots` live. A slot
  may only be retired when nothing pins it and no copy is landing in it; its
  memory then holds garbage, since other allocations reuse it.
- **Staging.** Disk to host and host to VRAM copies are asynchronous: started
  in one step, completed in a later one, with any amount of other activity in
  between. A host to VRAM copy reads its source when it completes, so a source
  recycled mid-copy lands garbage in the slot (while the engine still believes
  the slot holds the expert).
- **Slot table.** The engine's map from expert to VRAM slot. Routed launches
  resolve their slots through it at launch time.
- **Host tier and lookahead staging.** The host arena is split into the host
  cache (`CacheBufs`) and an optional lookahead staging area (`StageBufs`).
  Cache reads (demand or prefetch) use cache buffers, after the policy evicts
  an unpinned resident if none is free; lookahead reads into `StageBufs` use
  only free staging buffers, so they never evict a cache resident.
  Staging buffers feed host to VRAM copies like cache buffers and are
  recycled by eviction when unpinned.
- **Routing and launch.** The router picks up to `TopK` experts for the next
  layer. The layer launches once each routed expert is executable: in a ready
  VRAM slot (GPU path) or a ready host buffer (CPU expert path). Launched work
  runs behind the host in stream order, up to `MaxInflight` launches deep, and
  reads its locations until it completes.
- **Two modes** (`Mode`):
  - `"baked"`: routed launches are resolved on the host, and a captured CUDA
    graph bakes in the slots of up to `GraphMax` experts and may be replayed
    at any time until it is invalidated. Replays read exactly the baked
    slots. CPU expert execution is modelled here.
  - `"indirect"`: the whole decode step is one captured graph that bakes only
    the address of a device-side slot table (`dtable`, indexed by routed
    position, which is what a kernel loops over). Per layer the host resolves
    the routed experts and, once all are in ready VRAM slots, writes the
    table entries and releases the layer's flag (`ArmNode`, appending to
    `queue`). The GPU reaches armed nodes in stream order and reads the table
    when it executes (`GpuExec`), after which the work is in flight as in
    baked mode. **Handshake:** the host may overwrite a layer's table entries
    only once the GPU has consumed the previous node that reads them (no
    armed node waiting); meanwhile it is free to stage the next layer's
    misses. One layer is modelled, so consecutive nodes are consecutive steps
    of the same layer, which is exactly where the overwrite hazard lives. The
    CPU expert path is not modelled in this mode yet.
- **Policy.** Prefetch, placement changes and eviction are unconstrained
  nondeterminism: any expert may be staged at any time, any unpinned copy may
  be evicted at any time, graphs may be captured or dropped at any time. The
  invariants hold whatever the policy does, so policies can change freely
  without re-proving the protocol.
- **Pins.** A VRAM slot is pinned while in-flight work reads it, while a
  replayable graph bakes it in, while it holds a routed expert awaiting
  launch, or (indirect mode) while the device table points an armed,
  unexecuted node at it. A host buffer is pinned while in-flight (CPU path) work reads it,
  while it is the source of an in-flight copy, or while it holds a routed
  expert not yet in VRAM.

One layer's experts are modelled; layers share the same arenas but follow the
same rules, so more layers add states without adding behaviours.

### Invariants

| Invariant | Plain English |
|---|---|
| `KernelReadsValid` | Launched work only reads a location that holds the expert it was launched for, fully staged, for as long as the work is in flight. This covers both "never overwrite memory an in-flight kernel reads" and "never launch on a slot whose staging has not completed". Together with the launch guard it means every routed expert is resident when its layer launches. |
| `CopySourceValid` | The source buffer of an in-flight host to VRAM copy keeps the expert being copied until the copy completes. |
| `TableConsistent` | The slot table never maps an expert to a slot that does not hold that expert's fully staged bytes. |
| `GraphValid` | While a graph is replayable, every slot it baked in still holds the expert it held at capture. |
| `NoDuplicates` | No expert occupies two VRAM slots or two host buffers. |
| `TableReadValid` | (indirect) Whenever an armed node will read the device table for a routed position, the entry points at a slot holding that position's expert, fully staged. Covers "the table is filled before the GPU reads it" and "a slot referenced through the table is never reused". |
| `TableStable` | (indirect, action property) No step changes a table entry that an armed, unexecuted node can still read. |
| `TypeOK` | Variables stay in their domains. |

Capacity bounds are structural rather than an invariant: tiers are fixed
arenas of buffers, so exceeding capacity cannot be represented. The engine
follows the same rule (preallocated slot and buffer arenas, no growth).

Liveness: `RoutedLayerLaunches`, every routed layer is eventually launched,
and (indirect) `StepCompletes`, every armed node is eventually executed.
It assumes fairness only for the engine's demand path: copies and launched
work complete (weak fairness), and the demand actions (stage a routed,
non-resident expert, evict when a routed expert is waiting for room, launch)
eventually win against prefetch and policy activity (strong fairness, since
policy may keep stealing free slots). It also needs room: `small.cfg` has
`Slots >= TopK + GraphMax`. Checking it found one real subtlety: the demand
path must never count re-staging an already-resident routed expert as
progress, or prefetch and eviction can cycle forever.

Adding the staging area found a second one: weak fairness on "some copy
completes" is too weak. Lookahead reads into a staging buffer can keep
completing while a cache buffer's read never does, so the demand path never
gets room. The spec now requires every buffer's read and every slot's copy to
complete (per-buffer and per-slot weak fairness), which is what real I/O
gives. Per-copy fairness (one condition per expert, buffer and slot) is also
correct but made liveness checking intractable.

### Buggy variants

`Bug` selects a broken protocol. `run.sh bugs` checks each must be caught,
and each is caught by exactly the invariant it targets:

| `Bug` | Breakage | Caught by |
|---|---|---|
| `skip_kernel_pin` | Eviction ignores in-flight work | `KernelReadsValid` |
| `skip_copy_pin` | Eviction ignores in-flight copy sources | `CopySourceValid` |
| `skip_graph_pin` | Eviction ignores slots baked into a graph | `GraphValid` |
| `table_before_complete` | Slot table updated when a copy starts, not when it completes | `TableConsistent` |
| `skip_kernel_pin` (indirect) | Eviction ignores work in flight after the GPU read the table | `KernelReadsValid` |
| `write_before_consumed` (indirect) | Host writes a layer's table entries without waiting for the GPU to consume the previous node | `TableReadValid` (and `TableStable` alone, checked by hand) |
| `skip_table_pin` (indirect) | Eviction ignores slots referenced through the device table by armed nodes | `TableReadValid` |
| `retire_in_use` | A VRAM slot is retired (its memory reused) while work, a graph or the device table still references it | `KernelReadsValid` (baked), `TableReadValid` (indirect) |

### Results

TLC 2.19 (`tla2tools.jar` 1.8.0), 4 workers on the 4090 box, 2026-10-04.

| Config | Constants | Checks | Distinct states | Time |
|---|---|---|---|---|
| `small.cfg` (CI) | baked; 3 experts, 3 slots, 1 host buffer, TopK 2, MaxInflight 1, GraphMax 1 | safety + liveness | 30,226 | 18 s |
| `small_indirect.cfg` (CI) | indirect; 3 experts, 3 slots, cache buffer + staging buffer, TopK 2, MaxInflight 1 | safety + `TableStable` | 1,887,277 | 14 s |
| `live_indirect.cfg` (CI) | indirect; 2 experts, 2 slots, cache buffer + staging buffer, TopK 2, MaxInflight 1 | safety + `TableStable` + liveness | 21,116 | 10 s |
| `large.cfg` | baked; 4 experts, 4 slots, 3 host buffers, TopK 3, MaxInflight 2, GraphMax 2, symmetry | safety | 1,484,290 | 9 min 25 s |
| `large_indirect.cfg` | indirect; 4 experts, 4 slots, 2 cache buffers + 1 staging buffer, TopK 2, MaxInflight 2, symmetry | safety + `TableStable` | 1,945,070 | 8 min 00 s |
| `bug.cfg.in` x 9 | 3 experts, 2 slots, 2 host buffers, symmetry, baked or indirect | safety, must fail | n/a | about 6 s for all 9 |

`run.sh ci` takes about 38 s in total. Liveness for the indirect mode at
`small_indirect` size (3 experts, 3 slots) passes in about 5 minutes without
the staging buffer and did not finish within 9 minutes with it, hence the
separate smaller `live_indirect` config; with TopK 3 the indirect
safety model passed 4.7 million distinct states without finishing in 13
minutes, hence TopK 2 in `large_indirect`.

## Mapping to the engine

| Spec element | Engine component |
|---|---|
| `host`, `hostSt`, `HostPinned` | Host tier: fixed pinned-buffer arena with per-buffer state and pin counts |
| `CacheBufs`, `StageBufs`, `Lookahead` | Host arena split: the host expert cache, and a separate lookahead staging pool for next-layer prediction that only takes free staging buffers |
| `vram`, `vramSt`, `VramPinned` | VRAM tier: slot arena with per-slot state and pin counts |
| `live`, `Retire`, `Restore`, `MinSlots` | `ExpertSource::release_vram` / `reclaim_vram`: the main VRAM arena is allocated in chunks; a chunk is freed only after the open fetch finished, every copy completed and the compute stream synchronised (so nothing can pin its slots), and re-added empty; the tier keeps at least one layer's experts |
| `table` | Host-side slot table, expert to slot. `oominf-tiers` (being written now) implements this first, with host-resolved launches (baked mode without graphs) |
| `dtable`, `ArmNode`, `queue`, `GpuExec` | Device-side slot table for whole-step CUDA graphs (comes with CUDA graphs): per layer, a table of routed position to slot address read by the MoE kernels at run time; `ArmNode` is the host filling it and releasing the layer's flag (stream memop or mapped flag); `GpuExec` is the graph's kernel reaching that layer |
| `StageD2H` / `CompleteD2H` | Staging I/O: direct reads from the weight file into host buffers (io_uring completions). Includes `ExpertSource::prefetch`: decode predicts the next layer's experts and reads predicted disk misses into host cache buffers (evicting cold residents); the next fetch drains those reads before any copy, so a buffer is only copied from once `ready` |
| `StageH2V` / `CompleteH2V` | Host to device copies on a copy stream, completion observed by event |
| `Route`, `Launch`, `inflight`, `Complete` | Decode/prefill loop: router output, launch on the compute stream, completion events that release pins |
| `<<"cpu", h>>` launches, `InflightHosts` | Host compute (`oominf-cpu`): a decode-sized fetch may leave a host-tier record in its buffer and hand its address to the CPU workers instead of copying it. The buffer stays pinned for the rest of the fetch (it is one of the fetch's keys) and nothing writes host buffers before the next fetch or prefetch, which the MoE issues only after the CPU work completed (`Pending` also waits on drop) |
| `StageH2V` with a VRAM source | Stage promotions and decode victim saves: device-to-device copies on the same copy queue, enqueued before any refill of their source slot, so in-order execution of the queue keeps every copy source valid until it is read |
| `Capture`, `Replay`, `Invalidate`, `graph` | CUDA graph manager; capture registers graph pins, invalidation releases them |
| Unfair `StageD2H`/`StageH2V`/`Evict*` | Prefetch and placement policy plugins: free to do anything the pins allow |
| `Demand*` actions | Tier manager's demand path for the routed layer |

The rule for implementers: a pin is released only by the event that ends the
use (copy completion, launch completion, graph invalidation), and the slot
table is updated only by copy completion.

## Trace validation (planned)

The engine will emit a protocol event log in tests (stage start/complete,
evict, table update, launch with its resolved locations, completion, graph
capture/replay/invalidate), one event per spec action, carrying the same
arguments. A checker replays the log against the spec, either with TLC trace
validation (constraining `Next` to the logged action sequence) or with a Rust
reference model generated from these definitions, and fails if any logged
transition is not a legal spec step or any invariant breaks on the replayed
state. Any change to the tiering protocol updates this spec first and passes
`run.sh` and `run.sh bugs` before the code lands.
