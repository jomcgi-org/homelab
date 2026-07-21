# EmberVM Base Durability, Cross-Vendor Bases, and S3 Retention GC

**Status:** Proposed
**Created:** 2026-07-21
**Depends on:** ADR embervm/011 (vendor-bound warmth), ADR embervm/012 (cpu_sku stamping + grandfather rule), ADR embervm/014 (worker-authoritative state), R6 continuity verbs

---

## 1. Problem and verified current state

Base snapshots (the per-workload Firecracker rootfs + memory snapshot that every
cold boot, prime, and fresh instance restores from) have no durable copy and no
version GC. All facts below were verified live on 2026-07-21.

**The leak.** Node-4's scratch disk holds **245 base version directories, 301.7G
total**, under `/var/lib/embervm/scratch/embervm-noded/snapshots/bases/`, across
9 workloads at 25 to 29 superseded versions each (`bazel-query` 26,
`cold-image-demo` 28, `demo-postgres` 28, `hot-image-demo` 29, `sandbox` 28,
`sandbox-session` 28, `scratch-k8s` 27, `scratch-postgres` 25, `semgrep` 26).
Each dir holds `imageref`, `memfile`, `snapfile`. The disk is 1.8T, 58% used,
751.9G free, so this is not yet an emergency, but it is a pure leak: nothing
ever prunes a superseded base version.

**Why the existing eviction never fires (root cause).** The CP's BaseBuilder
DOES track superseded refs and DOES issue an eviction RPC once refcounts (primed
VMs + pinned sessions) drain to zero
(`control/lib/embervm/base_builder.ex:978-1100`). But it evicts via
`EvictSnapshot`, and noded's `EvictSnapshot` handler
(`noded/server/server.go:1369-1396`) dispatches only to the serving inventory
and then the SESSION bundle path (`RemoveSessionBundle`, which operates under
the `sessions/` warmth dir). A base ref like `bazel-query__0148fb2f0ac5` is in
neither inventory, so the eviction lands in the session path and no-ops against
a directory that does not exist, while `bases/<ref>` survives. Every superseded
base since R2 has leaked this way. (Verify during PR-3: confirm
`RemoveSessionBundle` returns idempotent success for an unknown ref, which is
why the CP never saw an error.)

**No durability.** The SeaweedFS `embervm` bucket holds ONLY `stateful` and
`volume` prefixes (verified via `weed shell fs.ls /buckets/embervm`). There is
no `base/` prefix in S3. The 301.7G on node-4 is the only copy of every base;
losing node-4's NVMe means rebuilding all 9 workloads' bases from scratch.

**The machinery already exists, bases were never wired in.** The R6 continuity
store (`noded/store/store.go`) is a complete S3 artifact client: meta.json
written LAST on export, SHA-256 verified restore, idempotent per checksum,
cpu_sku stamping. The server-side verb layer (`noded/server/store.go`) already
knows `ARTIFACT_KIND_BASE` end to end: key layout `base/<vendor>/<workload>/<ref>`
(`artifactPrefix`), local dir resolution to `SnapshotRoot/bases/<ref>`
(`artifactLocalDir`), and restore re-registration via `ReconcileBasesFromDisk`
(`reregisterRestored`). What is missing is purely call sites and policy:

- Nothing exports a base: the CP has NO `ExportArtifact` call site at all today
  (noted in `stateful_sweeper.ex` as unstarted "Task 10"), and noded's startup
  reconcile-export sweep (`enqueueReconcileExports`) deliberately covers only
  session/serving/stateful/volume/group kinds.
- `evictArtifactLocal` has no BASE arm (falls to `InvalidArgument`).
- The base NodeStatus message carries `BaseBuildState` but no `exported` flag
  (session/serving/stateful/group/volume all have one).
- No S3 retention of any kind exists for any artifact family beyond
  event-driven `EvictArtifact` on destroy/TTL; nothing lists or prunes by
  version count.

**Per-node CPU vendors (verified via /proc/cpuinfo on each node).**

| Node | Role | vendor_id | Model | Vendor token |
| ---- | ---- | --------- | ----- | ------------ |
| node-1 | master (etcd) | GenuineIntel | i5-12500T (Alder Lake) | `intel` |
| node-2 | master (etcd) | GenuineIntel | i5-12500T (Alder Lake) | `intel` |
| node-3 | master (etcd) | GenuineIntel | i5-12500T (Alder Lake) | `intel` |
| node-4 | worker (FC today) | AuthenticAMD | Ryzen 7 7800X3D (Zen 4) | `amd` |

The fleet is mixed: three Intel masters, one AMD worker. Today only node-4 runs
noded (DS + 2gi/16gi bricks); every existing base is AMD-cut.

**SeaweedFS capacity constraint (material).** The single volume server
(`seaweedfs-volume-0`, node-2) has **97.9G total, 41.0G free (58% used)** and
its volume-slot ceiling is saturated: `volume:130/130 active:90 free:0`. High
`delete_count`/`deleted_byte_count` on embervm-collection volumes suggests
compaction would reclaim space, but new volume allocation is impossible at the
current ceiling. Any base-store growth budget must fit inside existing writable
embervm-collection volumes until the ceiling or disk is raised. This is a
prerequisite work item (Phase 0), not a footnote.

**Current-set sizing (newest base per workload, live):** bazel-query 3.0G,
sandbox-session 2.0G, scratch-k8s 2.0G, semgrep 1.5G, hot-image-demo 768M,
cold-image-demo / demo-postgres / sandbox / scratch-postgres 512M each. Total
**~11.3G per vendor** for the current set. Both vendors, keep-2 retention:
~45G worst case, which does NOT fit in today's 41G free. Keep-1 both vendors
(~23G) fits. This drives the retention recommendation below.

---

## 2. Cross-vendor strategy: per-vendor bases (option b), already decided, now verified

The design fork was: (a) normalize CPUID via an FC CPU template so one base
hydrates on any x86-64 node, or (b) per-vendor bases keyed `(workload, vendor)`.

**Recommendation: (b) per-vendor bases. Option (a) is not available in
Firecracker.** Grounding:

- **FC capability:** Firecracker's static CPU templates are vendor-bound by
  construction: C3/T2/T2S/T2CL mask to Intel baselines and are Intel-only,
  T2A targets AMD Milan and is AMD-only. There is no template that produces a
  common Intel+AMD guest CPU identity, and snapshot restore across vendors is
  unsupported regardless of CPUID masking because guest-visible **MSRs and
  KVM vCPU state differ structurally between vendors** (a memfile captured on
  Zen 4 encodes AMD MSR state no Intel host can load). Custom CPUID templates
  (FC >= 1.4) do not change this: they mask CPUID leaves, not MSR/state layout.
- **The codebase has already committed to (b).** ADR 011 decision: "Warmth
  artifacts (memory snapshots, session banks, bases) are keyed by CPU vendor
  and never cross the vendor boundary; bases are built per vendor on the node
  that needs them." Implementation shipped in the R7/PR-E series: noded detects
  vendor from `/proc/cpuinfo` (`config.go:detectCPUVendor`), stamps
  `(CpuVendor, CpuTemplate)` into every SnapshotRef and store meta.json, and
  fail-closes on mismatch at both the driver restore path
  (`driver.go:780,1085`) and the store restore path
  (`server/store.go:cpuSkuMismatch`). Store keys are vendor-segmented
  (`base/<vendor>/<workload>/<ref>`), volumes deliberately excepted as
  vendor-portable. The CP stamps the anchor node's vendor on restores
  (`restore_vendor.ex`).
- **The current CPU template is a label, not a wire value.** Per
  `driver.go:96-110`, `Template` ("t2-conservative" on Intel, "amd-default" on
  AMD) is "a LOGICAL identity/versioning label only ... NOT yet wired into
  PutMachineConfig's wire-level cpu_template"; `PutMachineConfig` sets only
  vCPUs and memory (`driver.go:891`). So all 245 existing bases were cut with
  node-4's raw Zen 4 CPUID and are AMD-only in the strongest sense. The
  planned verify drill (boot + BuildBase + restore round-trip per vendor on
  real silicon, still outstanding on the masters) must run before any template
  name becomes load-bearing.

Consequences for this plan: one base build **per (workload, vendor tier)**, S3
holds one lineage per `(workload, vendor)` under the already-shipped key
layout, hydration is vendor-gated by the existing fail-closed sku checks, and
onboarding the Intel masters requires a first Intel build of each workload's
base (a one-time ~11G build cost amortized across all three masters, since any
Intel node can then hydrate from S3 rather than rebuild).

A homogeneous-Intel note: the three masters are identical i5-12500T parts, so
within the Intel tier snapshots are portable node-to-node even before any
template is wired. The conservative template still matters long-term (it
protects against a future non-identical Intel part joining), but it does not
block masters onboarding among themselves.

---

## 3. The base-durability design

S3 (SeaweedFS `embervm` bucket) becomes the authoritative durable base store;
node-local `bases/` becomes a cache; the CP owns both the durability intent and
the retention policy. This is ADR-014-aligned: base identity and desired
presence are CP intent (the Workload CR's `snapshotRef` is already the source
of truth for "current"); the node's disk and dial-home report remain
authoritative for what is actually present locally.

**Export (durability floor).** The CP, not noded, drives base export: after
BaseBuilder records a successful build (`snapshotRef` advanced), it issues
`ExportArtifact` (kind BASE) to the building node, and a periodic reconcile
re-issues it whenever NodeStatus shows the current base present-but-unexported.
This is deliberately NOT a noded-side blanket sweep: adding bases to
`enqueueReconcileExports` would ship all 245 leaked versions (301.7G) into a
41G store. Only CP-desired bases are exported. noded needs one additive change:
an `exported` flag on the base NodeStatus message, fed by the existing
`exportedCache`, mirroring the other artifact kinds.

**Hydrate (restore-instead-of-rebuild).** When BaseBuilder needs a base on a
node that lacks it locally, it first attempts `RestoreArtifact` (kind BASE,
vendor-stamped via the existing `RestoreVendor.stamp` helper); only on
not-present does it fall back to `BuildBase`. noded's restore path for BASE is
already complete (checksum-verified download into `bases/<ref>`, sku gate,
`ReconcileBasesFromDisk` re-registration). Because base refs are
content-addressed from the build signature (`<workload>__<hash>` where the
hash covers runtime digest + zip + sizing), a spec revert regenerates the OLD
ref, so **rollback becomes a hydrate, not a rebuild**, for any version still in
S3. Node replacement and brick cold-start likewise become hydrates.

**Local eviction (cache semantics).** Two fixes make local disk a real cache:

1. Fix the misrouted eviction: BaseBuilder switches its superseded-ref
   eviction from `EvictSnapshot` to `EvictArtifact{remote: false}` with a new
   BASE arm in `evictArtifactLocal` that removes `bases/<ref>` behind an
   in-use guard (no live VM restored from the ref, ref not BUILDING, ref not
   the registry-current base for its workload).
2. Make eviction reconciled rather than event-only (the event-only refcount
   path loses state across CP restarts, which is one reason 245 versions
   accumulated): a CP base-retention sweeper compares each node's reported
   base inventory against the desired set (current ref per workload, plus any
   ref still refcounted by primed VMs or pinned sessions) and evicts the rest.
   The one-time 301.7G backlog (245 dirs down to 9, reclaiming roughly 290G)
   drains through this same sweep; no special-case script.

Safety ordering: the sweeper refuses to evict any local base whose workload's
CURRENT base is not yet `exported` in S3, so the durability floor always lands
before the cache empties (durability-before-eviction, mirroring the volume
pairing guard's philosophy).

**S3 retention GC (CP-driven).** The CP prunes old base versions in S3 by
policy. Mechanism: a new thin `ListArtifacts` verb on noded (proxying an S3
LIST of a key prefix plus each artifact's meta.json `createdAtUnixMs`), and a
`vendor` field added to `EvictArtifactRequest` (mirroring
`RestoreArtifactRequest`) so the CP can address any vendor's prefix through
any connected node. The sweeper lists `base/<vendor>/<workload>/`, always
keeps the Workload CR's current ref, keeps the newest N-1 others by
created-at, and `EvictArtifact{remote: true}`s the rest. List-based rather
than CP-ledger-based so it is self-healing after CP restarts (orphaned S3
versions from a crashed export cycle get collected on the next sweep), which
is the same reconcile-from-observed-truth posture as ADR 014.

**Retention policy decision: keep N=2 versions per (workload, vendor)
(current + one predecessor), count-based, not age-based.** Rationale: bases do
not decay with age (an untouched workload still needs its current base
forever, so age-based retention is wrong-shaped); version count maps directly
to the utility bought (N=2 makes a one-step spec rollback a hydrate instead of
a rebuild, and rebuilds are minutes-cheap for anything older). Capacity
gating: N=2 across both vendors is ~45G worst case against 41G free today.

**Decided (Joe, 2026-07-21): N=1 per (workload, vendor), S3 holds the current
base only.** Rollback to a superseded version is a rebuild, accepted. N stays
one CP config value, so raising it later is a values change, not a design
change.

---

## 4. How this unblocks and sequences the R7 fleet rollout

R7/ADR 011-012 puts noded on all four nodes, with the Intel masters as the
cold/failover tier. Base handling is currently the missing prerequisite piece:

- **Without this plan**, each Intel node that onboards must cold-build all 9
  workloads' bases itself (~11G + build minutes each), repeated per node and
  again after any scratch loss, and node-4's AMD bases remain a single point
  of loss for the entire warm tier.
- **With PR-1/PR-2 shipped first**, the first Intel base build per workload
  (placed by BaseBuilder on any master) exports to `base/intel/...`, and the
  other two masters hydrate. Node loss anywhere becomes a hydrate. The AMD
  tier's bases become durable before the fleet grows, which is the right
  order: grow the store's floor before multiplying the nodes that depend on it.

Sequencing into the R7 program:

1. Phase 0 + PR-1 + PR-2 (durability + hydrate) before any master onboards.
2. The CPU-template verify drill on a master (boot + BuildBase + restore
   round-trip on Intel silicon, existing outstanding plan step) happens as
   part of the first master's onboarding, BEFORE Intel bases are treated as
   load-bearing. Decide there whether to wire a real `cpu_template` value
   (T2-family) into `PutMachineConfig` for Intel or continue with raw CPUID +
   label (the three identical i5-12500T parts make raw CPUID safe within the
   current tier).
3. BaseBuilder gains per-vendor build targets: one build per (workload,
   vendor tier present in the fleet), placed on the largest-budget instance
   OF THAT VENDOR (today it places on a single instance fleet-wide). This is
   the one genuinely new BaseBuilder behavior R7 needs from this plan.
4. PR-3 (local eviction + backlog reclaim) any time after PR-1/PR-2 verify
   live; PR-4 (S3 retention) after PR-1 has run long enough to produce
   version history.

---

## 5. Phased PR plan

Ordering rule: durability (PR-1) and proven hydrate (PR-2) land and verify
live BEFORE anything deletes (PR-3 local, PR-4 remote). Phase 0 gates PR-1.

**Phase 0 (GitOps, decided by Joe 2026-07-21; specified here, executed as its
own PR): second SeaweedFS volume server on node-4 + SeaweedFS-layer
replication.** Supersedes the compact-and-raise option. Specification:

- `projects/platform/seaweedfs-node4/`: a separate release of the upstream
  chart running ONLY a volume server (`global.masterServer` pointed at the
  existing master; master/filer/s3 disabled), pinned to node-4, dataDir a
  400Gi PVC with `maxVolumes: 350` (slot ceiling deliberately below disk
  capacity). Two render-verified collision traps the implementer must handle:
  set `nameOverride` (the chart names the volume Service/StatefulSet from the
  CHART name, so a second release otherwise renders the same
  `seaweedfs-volume` names as the live release), and set a distinct
  `global.serviceAccountName` (the chart creates the SA/ClusterRole
  unconditionally under the default "seaweedfs" name).
- `projects/platform/longhorn/storageclass-node4-ssd.yaml`: single-replica,
  strict-local Longhorn class on node-4's 3.7T ssd-01 disk. Deliberately NOT
  nvme-02: that is the same physical disk as the FC scratch tier, so the
  durable store would contend for IO and capacity with the workload it backs
  up. Single block replica because durability for this class comes from the
  SeaweedFS replication layer.
- `projects/platform/seaweedfs/values.yaml`: `global.enableReplication: true`
  + `replicationPlacment: "001"` (2 copies, different servers), so every NEW
  S3 volume pairs across the node-2 and node-4 servers.

A corrected premise found while specifying this: the node-2 volume server's
100Gi data PVC is ALREADY Longhorn-replicated (numberOfReplicas 2, healthy,
replicas observed on node-1 and node-4), so the store was not single-copy at
the block layer. The 001 flip still buys real things: new-volume capacity
(the old server is slot- and space-saturated), volume-server process
redundancy (reads survive a server outage, not just a disk loss), and
application-layer placement control. Physical copy count for NEW data is 3
(the node-2 server's copy on its 2-replica Longhorn volume, plus the node-4
server's copy on the 1-replica class); trimming the old server's Longhorn
volume to 1 replica is a later optimisation, allowed ONLY after the existing
000 volumes (whose only SeaweedFS copy lives in that server) have been
re-replicated.

Operational notes:

- Existing (pre-flip) volumes keep their creation-time replication 000 and do
  NOT gain a second SeaweedFS copy automatically. A manual operator pass is
  required in weed shell: `volume.configure.replication` (per collection or
  pattern) to restamp the desired placement, then `volume.fix.replication` to
  copy under-replicated volumes to the node-4 server. This moves tens of GB
  and runs only on an explicit go-ahead, never as part of the sync.
- With 001, new-volume allocation and writes to a 001 volume require BOTH
  volume servers up; reads survive either. There is a small window at sync
  time where the master restarts with 001 before the node-4 server is Ready:
  during it new-volume allocation fails while writes to existing writable
  volumes continue.
- Remaining ops item: add or verify a SigNoz alert on volume-server disk and
  slot saturation for BOTH servers.

**PR-1: Base export (proto + noded + CP).**
- proto: additive `exported` bool on the base NodeStatus message.
- noded: project `exported` for bases from `exportedCache` (the export verb
  itself already handles BASE). No sweep changes.
- CP: BaseBuilder issues `ExportArtifact` (kind BASE) after recording a
  successful build; periodic reconcile re-exports when the current base shows
  present-but-unexported in node facts. Op-log audit entry per export.
- Verify live: `base/amd/<workload>/<ref>` appears in S3 for all 9 current
  bases; meta.json stamped `amd/amd-default`.

**PR-2: Hydrate-on-miss (CP only).**
- BaseBuilder: before placing a `BuildBase`, attempt `RestoreArtifact` (kind
  BASE, vendor-stamped) on the target node; fall back to build on
  not-present/failed-precondition. Record restore vs build in the op-log.
- Verify live: evict one current base locally by hand (break-glass), watch the
  next demand hydrate it instead of rebuilding; confirm a spec revert hydrates
  the predecessor ref.

**PR-3: Local eviction fix + backlog reclaim (noded + CP).**
- noded: BASE arm in `evictArtifactLocal` (in-use + current + BUILDING
  guards); confirm the `RemoveSessionBundle` misroute hypothesis while here.
- CP: BaseBuilder superseded-ref eviction switches to
  `EvictArtifact{remote: false}`; new reconciled base-retention sweep of local
  inventories against the desired set, gated on the workload's current base
  being exported.
- Backlog drain (decided by Joe 2026-07-21): fix the misroute FIRST, then
  reclaim the ~290G backlog in ONE SHOT (not a bounded trickle), accepting
  the burst of delete IO beside live VMs.
- Verify live: node-4 `bases/` converges toward 9 dirs; disk usage drops
  ~290G; no wake/prime regressions (the readiness gate on advertised bases
  already protects placement).

**PR-4: S3 retention GC (proto + noded + CP).**
- proto: `ListArtifacts` verb (prefix in, refs + created-at out, bounded);
  `vendor` field on `EvictArtifactRequest`.
- noded: implement both (store client gains a LIST; evict honors explicit
  vendor over node-own vendor).
- CP: retention sweeper per (workload, vendor): keep CR-current + newest N-1,
  evict the rest remotely. N configurable, decided N=1.
- Verify live: superseded S3 versions collected within one sweep period;
  bucket size stabilizes around the retention envelope.

**R7 fold-in (separate PR series, already planned elsewhere): per-vendor
BaseBuilder targets + master onboarding + template verify drill**, sequenced
per section 4. This plan's PRs 1-2 are its prerequisite.

---

## 6. Decisions (Joe, 2026-07-21) and remaining open questions

Decided:

1. **SeaweedFS capacity path (Phase 0): second volume server on node-4 plus
   SeaweedFS-layer replication** (not compact-and-raise). Specified in the
   Phase 0 section (exact values/manifest changes, the corrected
   already-Longhorn-replicated premise, and the manual re-replication pass
   that pre-existing 000 volumes still need); executed as its own PR.
2. **Retention: N=1 per (workload, vendor), S3-backed.** Rollback to a
   superseded version is a rebuild, accepted.
3. **Intel cpu_template: wire and drill the T2-family template at master
   onboarding.** Attempt the real `PutMachineConfig` cpu_template value in
   the verify drill; keep it only if the boot + BuildBase + restore
   round-trip passes.
4. **Backlog drain: fix the EvictSnapshot base-misroute FIRST, then one-shot
   reclaim of the ~290G** (not a bounded trickle).

Still open:

5. **Does anything ever need a non-current base other than rollback?** With
   N=1 the S3 copy of a superseded ref is collected as soon as the sweep
   runs, so a pinned-session lineage outliving a base turnover relies
   entirely on the LOCAL refcount guard (the local sweep keeps refcounted
   superseded bases). Believed sufficient today (sessions pin refs and the
   local sweep honors refcounts); confirm before PR-4 flips on.
6. **Go-ahead for the existing-data re-replication pass** (weed shell
   `volume.configure.replication` + `volume.fix.replication`): when to run
   it, and which collections first (embervm, then the rest).
