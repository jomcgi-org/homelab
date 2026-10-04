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
specs/run.sh          # small (safety + liveness) and large (safety)
specs/run.sh small    # the CI config only
specs/run.sh bugs     # each buggy variant; every one must be caught
```

Needs Java 17+ and `tla2tools.jar` in `$TLA_HOME` (default
`/disks/nvme-02/src/.toolchains/tla`, from
https://github.com/tlaplus/tlaplus/releases). TLC runs nice'd on cores 8-15
with 4 workers. `MC.tla` wraps the spec to add symmetry for the safety-only
configs (symmetry is unsound for liveness, so `small.cfg` does not use it).

## ExpertTiering

### What is modelled

- **Tiers.** Every expert is always on disk. A fixed arena of host buffers and
  a fixed arena of VRAM slots cache copies. Each buffer or slot is `free`,
  `loading` or `ready`.
- **Staging.** Disk to host and host to VRAM copies are asynchronous: started
  in one step, completed in a later one, with any amount of other activity in
  between. A host to VRAM copy reads its source when it completes, so a source
  recycled mid-copy lands garbage in the slot (while the engine still believes
  the slot holds the expert).
- **Slot table.** The engine's map from expert to VRAM slot. Routed launches
  resolve their slots through it at launch time.
- **Routing and launch.** The router picks up to `TopK` experts for the next
  layer. The layer launches once each routed expert is executable: in a ready
  VRAM slot (GPU path) or a ready host buffer (CPU expert path). Launched work
  runs behind the host in stream order, up to `MaxInflight` launches deep, and
  reads its locations until it completes.
- **CUDA graphs.** A captured graph bakes in the slots of up to `GraphMax`
  experts and may be replayed at any time until it is invalidated. Replays
  read exactly the baked slots.
- **Policy.** Prefetch, placement changes and eviction are unconstrained
  nondeterminism: any expert may be staged at any time, any unpinned copy may
  be evicted at any time, graphs may be captured or dropped at any time. The
  invariants hold whatever the policy does, so policies can change freely
  without re-proving the protocol.
- **Pins.** A VRAM slot is pinned while in-flight work reads it, while a
  replayable graph bakes it in, or while it holds a routed expert awaiting
  launch. A host buffer is pinned while in-flight (CPU path) work reads it,
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
| `TypeOK` | Variables stay in their domains. |

Capacity bounds are structural rather than an invariant: tiers are fixed
arenas of buffers, so exceeding capacity cannot be represented. The engine
follows the same rule (preallocated slot and buffer arenas, no growth).

Liveness: `RoutedLayerLaunches`, every routed layer is eventually launched.
It assumes fairness only for the engine's demand path: copies and launched
work complete (weak fairness), and the demand actions (stage a routed,
non-resident expert, evict when a routed expert is waiting for room, launch)
eventually win against prefetch and policy activity (strong fairness, since
policy may keep stealing free slots). It also needs room: `small.cfg` has
`Slots >= TopK + GraphMax`. Checking it found one real subtlety: the demand
path must never count re-staging an already-resident routed expert as
progress, or prefetch and eviction can cycle forever.

### Buggy variants

`Bug` selects a broken protocol. `run.sh bugs` checks each must be caught,
and each is caught by exactly the invariant it targets:

| `Bug` | Breakage | Caught by |
|---|---|---|
| `skip_kernel_pin` | Eviction ignores in-flight work | `KernelReadsValid` |
| `skip_copy_pin` | Eviction ignores in-flight copy sources | `CopySourceValid` |
| `skip_graph_pin` | Eviction ignores slots baked into a graph | `GraphValid` |
| `table_before_complete` | Slot table updated when a copy starts, not when it completes | `TableConsistent` |

### Results

TLC 2.19 (`tla2tools.jar` 1.8.0), 4 workers on the 4090 box, 2026-10-04.

| Config | Constants | Checks | Distinct states | Time |
|---|---|---|---|---|
| `small.cfg` (CI) | 3 experts, 3 slots, 1 host buffer, TopK 2, MaxInflight 1, GraphMax 1 | safety + liveness | 30,226 | 14 s |
| `large.cfg` | 4 experts, 4 slots, 3 host buffers, TopK 3, MaxInflight 2, GraphMax 2, symmetry | safety | 985,516 | 7 min 38 s |
| `bug.cfg.in` x 4 | 3 experts, 2 slots, 2 host buffers, symmetry | safety, must fail | n/a | about 1 s each |

## Mapping to the engine

| Spec element | Engine component |
|---|---|
| `host`, `hostSt`, `HostPinned` | Host tier: fixed pinned-buffer arena with per-buffer state and pin counts |
| `vram`, `vramSt`, `VramPinned` | VRAM tier: fixed slot arena with per-slot state and pin counts |
| `table` | Slot table (host copy plus the device-side index buffer routed kernels read) |
| `StageD2H` / `CompleteD2H` | Staging I/O: direct reads from the weight file into host buffers (io_uring completions) |
| `StageH2V` / `CompleteH2V` | Host to device copies on a copy stream, completion observed by event |
| `Route`, `Launch`, `inflight`, `Complete` | Decode/prefill loop: router output, launch on the compute stream, completion events that release pins |
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
