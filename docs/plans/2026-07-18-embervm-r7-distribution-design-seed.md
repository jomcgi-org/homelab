# EmberVM R7 Distribution: design seed

**Status:** Design seed (findings capture, pre-spec). Nothing here is implemented.
**Origin:** 2026-07-18 deploy-freedom discussion after the /ember demo went public.
**Relates to:** ADR embervm/009 (ladder: R6 Continuity shipped gates-live-pending, R7 Distribution decided), ADR embervm/003 (artifact verbs), `docs/plans/2026-07-18-embervm-r6-continuity-spec-and-plan.md`.

## Problem

The /ember demo is now public and shared. Today a noded roll kills every live
microVM on the node, and the R6 drain machinery that should make rolls safe is
merged but undrilled. Deploys are frozen while the demo is being shown, which
inverts the goal: the platform should make deploys boring.

The near-term fix list (validate R6 for the demo lane) is small and listed at
the end. The larger finding is that a set of decisions, recorded here, turns
the single-node warm host into a 4-node distributed fleet with fast failover,
CP-owned rollouts, and a much smaller custom storage surface than R6 planned.

## Decision summary

| # | Decision | Replaces / affects |
|---|----------|--------------------|
| 1 | Heterogeneous fleet: noded on all 4 nodes; AMD node-4 = warm tier, Intel CP nodes = cold/failover tier | Dissolves the "R7 needs a 2nd FC node" hardware prerequisite |
| 2 | Vendor-keyed warmth: memory snapshots never cross the AMD/Intel boundary; bases built per vendor on-node | Placement gains a vendor axis |
| 3 | Volumes move to Longhorn (RWO, class-tiered replication, attach-as-lease) | Replaces the R6 custom vol.img S3 export for volumes |
| 4 | Longhorn snapshots + incremental S3 backup = the volume offload path | Replaces the proposed dm-thin / content-manifest diff pipeline |
| 5 | Warmth artifacts (memory snapshots, session banks, bases) stay on the R6 S3 store client, vendor-keyed, with export-gated local GC | R6 store client survives with a narrower role |
| 6 | Single-writer HA: CP arbitrates placement, Longhorn attach enforces it (fencing); generation blessing fences warmth artifacts | New invariant, must exist before the second warm-capable node |
| 7 | Attach latency: lazy detach while asleep, migratable handover for planned moves, overlapped attach on crash failover | Makes case 3 the only one that pays attach time |
| 8 | noded lifecycle: CP-sequenced rollouts (OnDelete semantics), condition-gated with a staleness bound | Replaces scheduler-timed Recreate/RollingUpdate |
| 9 | Exploratory: multiple right-sized noded pods per node, CP-driven binpacking, in-place pod resize as the signal to k8s, forecasted demand (TimesFM, TBC) | Future rung on top of 1-8 |

## 1. Heterogeneous fleet and the snapshot portability boundary

All 4 nodes have virtualization enabled and headroom. The constraint on using
the Intel CP nodes as microVM hosts was never KVM, it is Firecracker snapshot
portability, and the Firecracker docs are unambiguous:

- Cross-vendor snapshot restore is not supported: "Representing one CPU
  vendor as another CPU vendor is not supported."
- The T2CL (Intel) / T2A (AMD) CPU template pair provides instruction set
  feature parity so the same workload behaves identically on both vendors. It
  does not make memory snapshots portable.
- Even intra-vendor, the supported restore matrix is narrow (same CPU model
  family, some newer-host-kernel tolerance). "S3 makes snapshots durable and
  restorable on same-model hardware" is the accurate claim, not "portable."

Consequences, by artifact class:

| Class | Portability | Cross-vendor behavior |
|-------|-------------|----------------------|
| VOLUME | Fully portable (block data) | Travels anywhere; this is where durability lives |
| BASE | Rebuildable per vendor | Each node builds its own vendor-keyed bases (base builder already runs on-node; keys gain a vendor dimension) |
| SERVING / task warmth | Vendor-bound, disposable | Cross-vendor wake = cold start from the local base; only warmth is lost |
| SESSION | Vendor-bound and IS the state | Sessions are pinned to their vendor; they cannot move across the boundary |
| STATEFUL banked bundle | Memory part vendor-bound, volume part portable | Cross-vendor wake = fresh boot from target vendor's base + mount the volume (~1-2 s, within the accepted budget) |
| GROUP_SET | Warmth-only by contract | Rebuilds anywhere |

CPU templates are mostly ignorable in this design: since we never restore
cross-vendor, templates would matter only if a guest baked CPU-feature
dependent state into a volume (postgres does not).

The deploy story this buys: roll the AMD node, drain force-banks, a visitor
wake during the roll lands on an Intel node and cold-boots from the restored
volume. The page answers slowly once instead of appearing dead. Warmth
returns to the AMD node on the next bank/wake cycle after it is back.

## 2. Storage: Longhorn-native volumes

### What Longhorn replaces

The R6 volume export ships the whole vol.img per changed generation. Volumes
will exceed tens of GB and write amplification is a stated constraint, so an
incremental path is required. Three candidates were evaluated:

- Content-hashed block manifest (read + hash the whole image per export):
  rejected. Full-read cost scales with volume size and 4 MiB granularity
  amplifies postgres 8 KiB pages ~500x.
- dm-thin + thin_delta (kernel dirty-block tracking): viable, but it puts
  LVM/thin pool lifecycle inside noded (pool sizing, metadata, activation on
  restart, pool-fullness as a guest-write-error footgun) and reimplements by
  hand what Longhorn already ships.
- **Longhorn (chosen):** sync replication across nodes, snapshots as the
  bank-commit freeze point, and incremental backup to an S3 backupstore
  (changed 2 MiB blocks between snapshots; the write amplification is
  backup-side only, the live path is block-for-block). Restorable on any
  node or cluster. Four subsystems we do not have to build or operate.

Firecracker's memory side needs nothing new: native diff snapshots (dirty
page tracking, `snapshot_type: Diff`) make RAM banking incremental, with
periodic consolidation to bound chain length.

### Mechanics

- Pod volume mounts are fixed at creation and noded creates VMs dynamically,
  so guest volumes are NOT consumed as PVC mounts. The CP sets
  `spec.nodeID` on the Longhorn Volume CR (node attachment); the device
  appears as `/dev/longhorn/<name>` on the host; noded (already privileged)
  hands the block device straight to Firecracker (`path_on_host` accepts a
  device). Same shape KubeVirt uses for VM disks.
- Durability tiers are StorageClasses: replicated-HA (2 replicas) for
  critical/stateful demo workloads, strict-local single-replica for cheap
  scratch stateful. Matches the existing Longhorn replicas 1 / HA=2
  convention. A workload's spec names its class; the CP orchestrates and
  implements no storage mechanism.
- The "node PVC registry" is naming plus a watch, not a new store:
  workload -> volume is a deterministic name (the VOLUME ArtifactRef becomes
  the Volume CR name); volume -> node is the CR's own spec/status (the CP
  watches and patches it, needing RBAC on longhorn.io resources);
  node -> device is `/dev/longhorn/<name>` carried in the boot request.
- Distribution-for-availability is replicas of ONE volume, never
  snapshot-cloned PVCs (those create sibling volumes with divergent
  identities). Snapshot-cloned PVCs are the forking tool: fresh instance
  from a golden template, scratch copies.
- Per-volume overhead is real (engine/replica processes per volume). Fine at
  demo scale; record a ceiling before "every session gets a volume" is ever
  proposed.

### What stays on the R6 S3 store client

Vendor-bound warmth artifacts: memory snapshots, session banks, bases. They
are immutable blobs; an object store beats a block layer for them. Local
copies become a cache with export-gated TTL GC: evict only artifacts whose
store copy is confirmed current (noded already reports per-artifact
`exported` flags in NodeStatus), never evict un-exported ones. A pure TTL
recycler over an async write-back is a data-loss bug; the export-confirmed
flag is the gate.

### RWX rejected

Longhorn RWX is NFS via a share-manager pod, filesystem mode only, and
Firecracker needs a raw block device. Independently, multi-writer block on a
non-cluster filesystem is corruption by design. RWO attach exclusivity is
not a limitation to engineer around; it is the fence (section 3).

### The "Longhorn airlock" (recorded option, not planned)

Putting the LIVE guest volume on a replicated PVC closes the declared R6
bound "live writes since last bank commit are node-local" (RPO ~0 across
node death) at the price of iSCSI + sync replica writes on the serving hot
path. With Longhorn-native volumes this is no longer a separate mechanism,
it is just choosing the replicated StorageClass for the live volume, but the
latency cost must be measured against the demo's headline numbers before any
workload adopts it. For a workload that truly demands RPO ~0, app-level
replication (WAL streaming, CNPG-style) is the better long-term answer.
Spot semantics (bounded preemption, durability at bank granularity) remain
the v1 contract.

## 3. HA model: single arbiter, physical fence

Single-writer HA: one live instance ever, N stale standby caches. The
availability win is bounded failover time; the RPO is the last settled
generation. This must be stated plainly so nobody mistakes it for
active-active.

- **Election is free.** The CP is the single serializer; every wake goes
  through its single-flight path, so "who is live" is a row in CP state.
  No raft, no quorum among nodes.
- **Deciding is not enforcing.** From the CP's chair, "node dead" and "node
  partitioned but alive and writing" are indistinguishable. The CP's
  authority over an unreachable node is advisory. Longhorn attach
  exclusivity converts the placement decision into a physical fact:
  attaching the volume to the new node invalidates the zombie's access, and
  its writes fail instead of corrupting. Attach IS the fencing token
  (Kleppmann's argument; also exactly how Kubernetes fences StatefulSets
  against zombie kubelets via RWO attachment).
- The adoption design (nodes deliberately outlive CP authority gaps) makes
  the CP-is-briefly-wrong window a designed-in state, and it has produced
  two real bugs already (orphaned primed VMs, stale group bridge records).
  Fencing makes CP wrongness survivable for the one state class where
  wrongness is unrecoverable.
- **Generation blessing** fences the warmth-artifact plane: the CP is the
  sole issuer of generation numbers; a node reporting an artifact generation
  the CP never blessed discards it instead of exporting it. Monotonic
  counters as fencing tokens. This invariant must exist before the second
  warm-capable node, it is trivial to enforce from day one and miserable to
  retrofit after a split brain.
- "Dirty" nodes are just STALE caches: local artifact generation behind the
  blessed generation. Restore-on-miss (local > store > cold, shipped in R6)
  is the lazy catch-up path; push pre-warm is the same replication with an
  eager trigger, added per-workload as a latency knob once miss cost is
  measured.

## 4. Attach latency, by case

| Case | Frequency | Mechanism | Attach cost |
|------|-----------|-----------|-------------|
| Same-node relight | Overwhelmingly common | Lazy detach: the volume stays attached to its node while the VM sleeps (attachment is node-to-volume, not VM-to-volume; idle cost is one engine process) | Zero |
| Planned move (roll, drain, preemption with notice) | Per deploy | Longhorn `migratable: true` coordinated dual-attach handover, sequenced by the CP during the drain window (source is banked before target relights, so single-writer holds). This is how Harvester does KubeVirt live migration on Longhorn | ~Zero |
| Crash failover (node death) | Rare | Fresh attach (~2-5 s), fired as the FIRST step of the wake plan, overlapped with warmth fetch, tap setup, and Firecracker spawn; the device is needed only at drive-config time | Seconds, off the critical path's sum |

Supporting knobs: replica locality (keep a replica on the likely failover
node via dataLocality/replica scheduling) so a new engine connects to local
data; the Longhorn v2 (SPDK) engine as a future upgrade for attach and
datapath latency, not yet maturity-proven enough to bet the demo on.

`migratable` is also the door to true live migration later: the storage half
of the "zero-interruption rolls" tier ADR 009 deferred is already solved in
the stack; only Firecracker memory transfer would remain.

## 5. noded lifecycle: CP-sequenced rollouts

Scheduler-timed rollouts (Recreate today, RollingUpdate maxUnavailable on a
DaemonSet later) cannot express what the fleet needs: pause rolls when the
store is unhealthy, drain the cold tier first, pre-warm before break, wait
for an idle-bank window. PDBs do not apply to same-controller rolling
updates. Condition-based teardown requires OnDelete semantics plus the CP as
rollout controller:

- ArgoCD still delivers the new pod template (GitOps unchanged); k8s remains
  the reconciler ("recreate whatever gets deleted, with the new spec").
- The CP notices template-generation drift, then per node: check interlocks
  (store healthy, no wake in flight, export backlog sane, idle-bank window)
  -> drain -> confirm banked -> delete pod -> wait for the new noded to
  reconcile and report -> next node.
- **This pays off on the single node TODAY:** a merged embervm PR no longer
  Recreates noded the instant ArgoCD syncs, potentially mid-demo-wake. The
  CP rolls in the demo's natural idle-bank window (idleBank 600 s) and can
  run a relight self-check before declaring the roll done. Most deploys get
  zero visitor-visible impact; the "paused for deployment" page flag covers
  the rare overlap.
- **Guardrails (required, not nice-to-have):** a staleness bound (if
  interlocks never go green, force the roll after N hours and alert;
  condition-gated automation without a forcing deadline becomes a silent
  freeze discovered as "noded is running a 6-week-old image") and a CP-down
  fallback (max-generation-lag alert; manual pod delete as the escape
  hatch). Expect ArgoCD to show Synced while pods lag the template; that is
  correct OnDelete behavior and belongs in the argocd-outofsync runbook.

Division of labor, which is the recurring theme of this whole design: the CP
owns WHEN; k8s, Longhorn, and S3 own HOW.

## 6. Traffic distribution

The CP is already off the data path: serving traffic flows Envoy -> noded
DNAT -> tap; the CP appears only on the miss path (activator fallback) and
at xDS publish time. Therefore:

- Busy-hours stateful capacity ("PG busy 9-5 Mon-Fri") decomposes into a
  time-based warmth policy (min-warm schedule so it never banks during the
  window, pure CP policy) plus, when read replicas exist, EDS fan-out
  (publish N weighted endpoints; structurally the xDS sidecar already does
  this).
- CNPG-style postgres clustering (separate read replicas) is app-level WAL
  replication, orthogonal to the block/artifact layer, and is parked as a
  future optimization. Single-writer plus fast relight covers demo-scale
  for a long time.

## 7. Exploratory rung: right-sized noded pods and forecast-driven binpacking

Moving off "one big node-pinned pod" opens a further step, recorded here as
exploratory (not part of the R7 core):

- Multiple smaller noded pods per node are structurally sound: Firecracker
  processes are noded's children, so they live inside the pod cgroup and a
  pod's requests/limits genuinely bound its VMs; each pod has its own netns
  (bridges, taps, DNAT), so co-resident noded pods do not collide. This also
  gives sub-node roll granularity.
- The CP knows total workload requirements and node headroom, so it can
  binpack VM placements and declare the resulting per-pod footprint to k8s
  via in-place pod resize (the `resize` subresource).
- Forecasted demand (TimesFM, TBC) feeds the same placement seam as the
  time-based warmth policy: pre-warm and pre-size ahead of the 9 am ramp.
  TimesFM runs as its own pod or sidecar, not as a Firecracker guest: it is
  an ideal FC candidate on paper but ~2 GB of resident model memory does not
  belong on the critical path's warm budget.
- **Resize is GA on our cluster (k8s 1.35)**, so the feature-gate question
  is resolved. The ordering invariant: noded is the enforcement point (VMs
  are cgroup children of the noded pod), so changes flow
  noded-confirms -> CP -> k8s resize. The CP declares footprint to k8s
  after noded's actual packing changed, never as a request that placement
  depends on. One direction-specific caveat: when growing near node
  headroom, reserve via resize before booting the VM, or a denied resize
  leaves the node overcommitted; when shrinking, unpack first, then
  declare down.
- The two-schedulers tension must be resolved as: the CP is the sole
  decider of VM placement, k8s is kept informed; never the reverse, or
  headroom gets double-booked.

## 8. Near-term: protecting the demo (unchanged priorities)

Independent of everything above, "deploy freely this week" is:

1. **Drill the demo-protection subset of the R6 gates now** (gates 2, 4, 5,
   8: noded roll with live scratch-postgres banks within the window, relit
   on next wake, pre-roll row survives; wake mid-drain parks and resolves;
   CP roll mid-drain converges). These drills involve nothing composite, so
   the R5 gate-1 entry criteria does not block them as a standalone
   demo-protection drill; they do not claim R6 closure.
2. **Fix the SeaweedFS `embervm` collection** ("No writable volumes for
   collection:embervm"): no R6 export has ever succeeded, so the durability
   seam has never worked end to end. Under this design it remains the
   backing store for warmth artifacts and becomes the Longhorn backupstore
   target as well.
3. **Surface drain state on the page:** plumb the existing NodeStatus
   `draining` flag through `/v1/stateful/demo-postgres` -> ember_public
   status -> a "paused for deployment" chip and dot state (the page already
   polls this endpoint; no new polling).
4. **Deploy-time canary:** after an embervm sync goes Healthy, drive one
   real wake + insert + aggregate cycle and alert on failure. The passive
   /health check deliberately never wakes the VM, so a broken relight path
   (the exact failure mode of a bad promotion) is otherwise invisible until
   a visitor hits it. Promote the drain-related dry-run alerts alongside.
5. **Ship the parked branch** `fix/embervm-group-bridge-addr-idempotent`
   (A2 + entry-DNAT E fix): composite-lane only, no demo impact, and it
   doubles as the first supervised exercise of the roll-drain path.

Expectation-setting: with today's machinery a noded roll is ~30-90 s of the
demo showing "asleep" (drain hold + image pull + reconcile), during which
the status endpoint keeps answering (it reads the CP, never the node) and a
visitor wake parks until the new noded is up. Nothing breaks; wakes are
slower. The CP-sequenced lifecycle (section 5) is what turns this into zero
visitor-visible impact for most deploys.

## Open validations

| # | Question | How to answer |
|---|----------|---------------|
| 1 | Longhorn attach/detach behavior under embervm churn (CR-driven node attachment, engine restart on noded roll) | Bench harness on a scratch volume |
| 2 | Replicated-class data-path latency vs raw nvme (demo pg insert/aggregate, relight time) | Run demo-postgres volume on a 2-replica PVC on node-4; compare |
| 3 | `migratable` dual-attach handover semantics under CP sequencing | Drill a planned move |
| 4 | Longhorn incremental backup cadence vs bank-commit frequency (backupstore load, chain length) | Measure on the demo workload |
| 5 | In-place resize memory-decrease behavior in practice (GA on 1.35; the availability question is resolved, decrease semantics still deserve a live check) | Resize a scratch noded pod down under load |
| 6 | Intel-node noded prerequisites (kvm-intel, scratch layout, noisy-neighbor blast radius next to CP/etcd) | Staged bring-up on one CP node |
| 7 | R6 gate 2/4/5/8 demo-protection drill results | Drill per section 8 |

## Rejected alternatives (with the one-line reason)

- DaemonSet RollingUpdate maxUnavailable as the rollout mechanism: cannot
  express store-health pauses, tier ordering, or idle-window timing.
- dm-thin / thin_delta custom diff pipeline: reimplements Longhorn's
  replication + snapshot + incremental backup by hand inside privileged
  noded.
- Content-hashed block manifest export: full-read cost scales with volume
  size; write amplification at practical block sizes.
- btrfs/ZFS send streams: reformats the scratch tier and couples restore to
  a filesystem, buys nothing over the chosen path.
- RWX / shared-block volumes: Longhorn RWX is NFS (no raw block device for
  Firecracker); multi-writer block is corruption by design; and pre-attached
  write access everywhere dismantles the fence that makes single-writer HA
  safe.
- Pre-attach-everywhere for instant failover: trades seconds in the rare
  crash case for reintroducing the split-brain problem in every case.
