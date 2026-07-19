# EmberVM Design Seed: Artifact-Decoupled noded (Stateless Daemon, S3-Distributed Artifacts)

**Status:** Design seed for review, not scheduled work. Extends the R7 Distribution seed
(docs/plans/2026-07-18-embervm-r7-distribution-design-seed.md) and ADR embervm/009's S3
durability seam. Written 2026-07-19 after the bazel-query base-build night surfaced the
costs of the current coupling. Amended 2026-07-19 after review with Joe: added the
storage-tier taxonomy, the rollout topology (surge rolls now, control-plane-managed
node deployments as the target), and resolved open questions 1 and 3. Second review
pass the same day resolved relay handover, placement authority, the drain budget
(1m50s), GC policy, backfill scheduling, and store durability delegation (resolved
questions 3 to 8).

## Problem

Today noded's boot and the artifact lifecycle are welded together:

- **rootfs bakes ride pod init.** Seven rootfs-builder init containers run on every noded
  pod start, re-baking every workload's guest OCI image into an ext4. Measured cost
  tonight: 4 to 5 minutes of init per roll, paid on every chart merge whether or not any
  image changed.
- **Base snapshots are built on the serving daemon at admission**, inside the pod
  lifecycle. A noded or control-plane roll mid-build kills the build VM; tonight this
  destroyed in-flight bazel-query warmings repeatedly, and the failure modes (silent
  10s gRPC deadline, wedged BaseBuilding status) took hours to pin because build,
  serve, and boot share one process.
- **The image registry is env config.** EMBERVM_NODED_IMAGES is rendered from chart
  values into the deployment, so adding or changing a workload's image forces a full
  noded roll (and therefore the two costs above), even when the daemon binary is
  unchanged.
- **Durable (banked) artifacts already export to S3 async after bank commits (R6), but
  boot/wake still expects them local**; the restore-on-miss path is the part of R6 that
  makes node loss survivable and it must be the norm, not the fallback.

## Direction

noded becomes a **stateless executor**. All artifacts live in the S3 store (SeaweedFS
today, endpoint already configurable) and are **distributed by the control plane**;
noded fetches on instruction or on miss and caches on NVMe. A fresh noded boots EMPTY:
no init containers, no baked-in registry, just gRPC up, capacity + identity reported,
then the control plane pushes what the node should carry.

### Artifact taxonomy, keying, and who builds what

| Artifact | Compatibility key | Built by / where | Trigger | Boot behavior |
| -------- | ----------------- | ---------------- | ------- | ------------- |
| rootfs (ext4 per guest image) | `(image_digest, arch)` | CI or any builder; baking is pure OCI layer extraction, NO code from the image executes, so any machine can bake any arch | Control plane (or CI on image publish) | noded fetches from S3 on registry push or first use; digest-keyed NVMe cache |
| Base snapshot (memfile + snapfile from a warming BuildBase) | `(base_key, cpu_sku)` where base_key = workload + image digest + revision | A noded in **builder role** on a node whose CPU SKU matches the serving target; Firecracker snapshots only restore on a compatible CPU | Control plane, out of band from serving (build queue, not admission-inline) | noded RestoreArtifact from S3 on miss; control plane pre-warms per placement policy |
| Durable snapshots (session banks, stateful generations, serving banks, group sets, volumes) | `(kind, workload, ref, cpu_sku)` | The serving noded itself: banking stays a data-plane action at sleep/idle, control plane stays off the hot path | noded triggers the bank on sleep as today; flush to S3 is the async export after the bank commit (R6, shipped) | wake restores from S3 on local miss; local eviction only after export is confirmed present in the store |

Key clarification the table encodes: **arch and CPU SKU are different constraints.**
A rootfs is portable across every machine of its architecture and can be baked
anywhere. A memory snapshot is pinned to the CPU model family that cut it (the restore
path already checks the snapshot CPU vendor). So rootfs building escapes the fleet
entirely, while snapshot building escapes the *serving pod* but not the CPU SKU.

### Empty-boot noded and the pushed registry

- Delete the rootfs-builder init containers and EMBERVM_NODED_IMAGES.
- New node verbs (names indicative): `SyncRegistry(entries)` idempotently declaring the
  full set of (workload, image digest, rootfs artifact ref, harness init, sizing) a node
  should carry, plus incremental `RegisterWorkload` / `DeregisterWorkload`. The registry
  lives in noded memory only; the op-log stays the source of truth and the control plane
  replays the registry on every daemon (re)connect, exactly like the existing primed-VM
  adoption handshake.
- noded's NodeStatus grows `cpu_sku` (vendor + family/model, or the Firecracker CPU
  template in force) alongside the existing arch and headroom facts. Every snapshot
  artifact is stamped with the cpu_sku that cut it, and restore refuses a mismatch
  loudly (today only the vendor is implicitly checked at load).
- Placement and distribution become a control-plane concern with real information:
  "which nodes hold artifact X locally, which could fetch it, which CPU SKUs can restore
  it". This is the R7 placement seam; a placement move is `RestoreArtifact` on the
  target plus `EvictArtifact` on the source, never a rebuild.

### What already exists (do not rebuild)

- `ExportArtifact` / `RestoreArtifact` / `EvictArtifact` verbs with the Fork-3 key
  scheme, meta.json-written-last completeness marker, checksum verification, and the
  configurable S3 store client (ADR 003 verbs, generalized and shipped through R6).
- Async export after bank commits, and export-confirmed-only local eviction.
- BuildBase itself (boot, health-gate on guest-owned readiness, snapshot) is sound; the
  problems were its *placement* (inline with admission, inside the serving pod) and its
  client deadline (fixed 2026-07-19, explicit 10m).
- NodeRegistry's per-daemon stream and reconnect handling, which the registry replay
  piggybacks on.

### Storage tiers on the node

noded's disk splits into three tiers with different durability answers. Only the third
tier ever gets a real PVC.

1. **Reconstructible cache** (rootfs ext4s, base snapshots): the S3 store is the master
   copy; the node holds a digest-keyed cache on local NVMe. This stays **hostPath (or a
   local PV), deliberately**. A per-pod ephemeral PVC would empty the cache on every
   roll and turn the init tax into a fetch tax; a Longhorn PVC would pay network
   replication to protect data whose durable copy already lives in the store. Node-local
   persistence across pod restarts is a feature: during a surge roll the old and new pod
   share the cache, so the new pod comes up warm for free.
2. **Durable artifacts** (session banks, stateful generations, serving banks, group
   sets): local NVMe plus async S3 export plus evict-only-after-export-confirmed.
   Shipped in R6, already correct, no PVC needed; S3 is the durability layer.
3. **Live durable volumes** (pg data and other guest-attached durable storage): the one
   tier where a PVC belongs. Longhorn-native volumes with attach-as-fence, per the R7
   Distribution seed. Attach moves with placement; the volume outlives any node.

**Rejected: a shared read-only PVC for artifacts with a writer-privileged builder.**
It reads as simpler but loses on every axis: Longhorn RWX is an NFS share-manager pod,
a single point of failure in front of every node's artifact reads with mediocre
large-sequential-read performance; every node sees every artifact instead of caching
only what it serves; and it introduces a second distribution path (filesystem
consistency semantics alongside the store's meta.json-last contract). The S3 store plus
per-node cache strictly dominates: partial per-node sets, no attach choreography, a
well-defined degraded mode, and the builder writes through the existing store client
instead of needing privileged attach semantics.

### Rollout topology: surge rolls now, control-plane-managed deployments as the target

Today noded is already a Deployment (not a DaemonSet): single replica, pinned to
node-4 by nodeSelector, `strategy: Recreate` with the comment "never run two against
one node". Zero-downtime rolls mean dissolving that constraint so two noded pods can
coexist on one node for a handover window. Pausing guests is acceptable; losing
warmth is not.

**What zero-downtime means per workload class.** Firecracker has no live migration
(no pre-copy memory streaming); the migration primitive is pause, snapshot, move
files, restore. A cut snapshot is inert data and moves freely within an
(arch, cpu_sku) compatibility class, so cross-node warmth moves are supported; only
zero-pause moves of a running VM are off the table, and nothing here needs them.

- **Serving workloads**: true zero downtime via overlap. The new pod primes from base
  snapshots out of the shared node cache, the relay shifts traffic, the old pod drains.
- **Sessions / stateful**: brief pause, never loss. The old pod banks on drain (the R6
  sleep path), the new pod relights from the shared node cache. Seconds, not rebuilds.
- **In-flight builds**: preStop drain short-term (Phase 0); structurally, out-of-band
  builds (Phase 3) mean serving rolls never kill builds at all.

**Step 1, surge-safe pod spec, in the chart, shippable early.** Partition per-instance
mutable state under nvmeRoot (VM run dirs, vsock CID ranges, TAP naming) by pod
instance so two daemons never collide, while the artifact cache dirs stay shared
(digest-keyed content is safe for concurrent readers; meta.json-last covers the
writer). Readiness means "adopted by the control plane and registry replayed";
traffic must never reach a pod before that gate. preStop means "bank sessions, drain
builds, confirm exports". Then flip Recreate to `RollingUpdate` with `maxSurge: 1,
maxUnavailable: 0`. This ships zero-downtime rolls on node-4 with plain Kubernetes
primitives, and every line of it is the pod template the control plane later stamps.

**Target, the control plane authors per-node Deployments on tainted nodes.** No
intermediate per-node Helm templating layer (a values list of nodes would have exactly
one customer today and gets deleted when the control plane takes over). The control
plane creates and deletes one noded Deployment per registered node, tolerating the FC
node taint, and owns roll choreography: surge the new pod, replay the registry, wait
for banks to confirm, release the old. This is the standard operator pattern; the
GitOps rule forbids humans mutating the cluster, not controllers doing their job.
Ground rules:

- **Git stays the source of truth for versions.** The desired noded image digest is
  chart-delivered control-plane config; a chart bump still drives every roll, the
  control plane is the actuator that rolls node-by-node (and can canary one node).
- **Gated on Phase 2, clean after Phase 5.** While EMBERVM_NODED_IMAGES and the init
  containers exist, a control-plane-authored pod spec must carry per-workload env,
  dragging chart values into controller code. After the pushed registry and empty
  boot, the pod spec is workload-agnostic (image digest, node, NVMe mount), the shape
  a controller stamps trivially. Building the controller before that means building
  it twice.
- **Bootstrap asymmetry**: the control plane itself stays a plain Helm/ArgoCD
  Deployment forever, schedulable off the tainted FC nodes, so a wedged fleet can
  never take down the thing that repairs it.
- **ArgoCD hygiene**: control-plane-created Deployments live outside the Argo app
  (untracked foreign resources, like any operator's children), with ownerReferences
  chaining to a control-plane-owned parent so GC cleans up forgotten nodes.
- **Taint FC nodes** (`embervm.jomcgi.dev/node=true:NoSchedule`) so only noded,
  builder, and relay pods land there and general workloads never compete with guest
  memory. Valuable independent of everything else; can ship in Phase 0.

## Phases (each independently shippable)

**Phase 0, quick wins (can ship this week, no design risk):**
- Digest-keyed rootfs bake cache: init containers become a no-op when the target ext4
  for the image digest already exists on NVMe. Kills the 4 to 5 minute tax on rolls
  where images did not change.
- preStop drain: noded finishes or cleanly aborts in-flight BuildBase work inside the
  termination grace period instead of orphaning half-built VMs.
- Taint the FC nodes and add tolerations to noded and the serving relay, locking
  general workloads off guest-memory hosts.

**Phase 1, rootfs via store:** CI (or a cluster Job) bakes ext4 per (image digest,
arch) on image publish and exports it as a new artifact kind (ROOTFS) through the
existing store client. noded fetches + caches on demand. Init containers deleted.

**Phase 2, pushed registry:** SyncRegistry/Register/Deregister verbs; control plane
replays the registry on daemon connect; EMBERVM_NODED_IMAGES deleted. Adding a workload
no longer rolls noded at all.

**Phase 3, out-of-band base builds:** BaseBuilder becomes a build queue that (a) checks
the store for (base_key, cpu_sku) first, (b) schedules the build on a matching-SKU node
with headroom (with one node this is still node-4, but no longer inline with admission),
(c) exports, then (d) distributes per policy. Serving Prime falls back to
RestoreArtifact on local miss.

**Phase 4, durable restore-on-miss everywhere:** audit every wake/relight path
(session, stateful, serving, group) for the store fallback and the cpu_sku gate;
this is the R6 promise made uniform.

**Phase 5, empty boot:** remove the last boot-time couplings so a node joins the fleet
cold and the control plane warms it per policy. This dissolves into R7 proper
(vendor-keyed warmth, multi-node fan-out) and is where the second Firecracker node
(R7's hardware prerequisite) starts paying rent.

**Phase 6, surge rolls (can ship early, independent of Phases 1 to 5):** partition
per-instance state under nvmeRoot (keyed by the control-plane-issued instance id),
readiness gate on registry-replayed-and-adopted, preStop bank/drain inside the 1m50s
budget, then Recreate becomes RollingUpdate maxSurge 1 / maxUnavailable 0.
Zero-downtime rolls on node-4; traffic never reaches an unready pod.

**Phase 7, control-plane-managed node deployments:** the control plane authors one
noded Deployment per registered tainted node and owns roll choreography; the chart's
static noded Deployment is deleted. Gated on Phase 2 (clean after Phase 5); the pod
template is Phase 6's, unchanged.

## Invariants and risks

- **Hit/miss invariant holds:** exports, restores, registry pushes are lifecycle
  actions; the request hot path never waits on S3.
- **Store availability becomes a cold-boot dependency.** A node with a warm NVMe cache
  must keep serving what it has when SeaweedFS is down (degraded mode: no new fetches,
  no evictions); an empty node simply stays empty until the store returns. Never fail
  from warm to cold because the store blinked.
- **Single writer per artifact key** (build queue serializes per base_key + cpu_sku),
  and meta.json-last stays the completeness contract.
- **CPU SKU gate must fail loudly** at restore time with the mismatch in the error, not
  wedge like tonight's silent kills.
- **Registry replay must be idempotent and complete** on every reconnect; a node that
  missed a Deregister must converge to the pushed set (SyncRegistry is authoritative,
  incrementals are optimizations).
- **Traffic never reaches an unready noded.** Readiness means registry replayed and
  control-plane adoption complete; the surge handover kills the old pod only after the
  new one passes that gate and banks are confirmed.
- **The artifact cache is node-scoped, not pod-scoped.** Rolls must inherit the warm
  cache; any storage change that empties the cache on pod restart is a regression.

## Resolved questions (2026-07-19 review)

1. **ROOTFS artifacts live in the S3 store**, not GHCR OCI artifacts: one distribution
   path, one auth story, one completeness contract, and the store client already
   exists. CI or the builder pushes through the same seam as every other artifact.
2. **Builder role: same noded binary, builder flag, separate pod**, scheduled by the
   control plane on a matching-SKU tainted node, writing through the existing store
   client. Serving rolls never kill builds; no shared-volume attach semantics needed.
3. **Relay handover during surge is control-plane-driven.** The control plane owns the
   xDS layer: on a roll or preemption it shifts serving endpoints to the new instance
   and drains the old one, holding or re-queueing in-flight sync-wait requests within
   the drain budget. Prerequisite: preemption/SIGTERM must reach the control plane
   immediately (event, not polled status), so coordination starts at notice time.
4. **Daemon authority flips: daemons never claim nodes, the control plane places
   instances.** noded reports identity and capacity; the control plane decides what
   runs where and issues each instance its identity (an epoch/instance id assigned at
   placement). Two instances on one node therefore exist only because the control
   plane asked for it (surge handover, builder pod), and it always knows which is
   authoritative. CID ranges and TAP naming derive from the issued instance id, which
   settles the surge partitioning mechanics.
5. **Drain wall-time budget is 1m50s**, targeting spot-instance compatibility (2m
   notice minus notification latency). Priority order inside the budget: durable banks
   first, serving banks second, abort in-flight builds on clock expiry (builds are
   reconstructible by definition). terminationGracePeriodSeconds stays budget + 30s
   per the existing convention.
6. **GC is control-plane-owned, and the policy splits by tier.** Reconstructible
   artifacts (rootfs, base snapshots): registry-referenced entries are pinned,
   unreferenced ones TTL out by age (order 1h; worst case is a re-bake), with a
   bounded +n allowance for workloads that snapshot prolifically. Durable artifacts
   (banks, generations) are never TTL'd: they are ref-counted from the op-log and
   deleted only when the owning session/generation is gone. Age is the wrong signal
   for data that cannot be rebuilt.
7. **rootfs baking and backfill are control-plane-scheduled Jobs, not CI.** The
   control plane knows the node inventory (arch, cpu_sku), the workload registry, and
   the store contents, so a registry entry whose artifact is missing from the store
   IS the work signal: it schedules a bake Job on any node of the right arch (baking
   needs no KVM) and snapshot builds on SKU-matched FC nodes, pull-based. No
   CI-to-store network path, no publish webhook; this unifies with the Phase 3 build
   queue as one mechanism for builds and backfills.
8. **Store durability is delegated to the backend behind the S3 endpoint.** The store
   client speaks plain S3 API with a configurable endpoint (ADR 009 chose SeaweedFS as
   the first backend, not the architecture), so a production deployment points at real
   S3/R2/B2 and inherits its durability with a values change. The homelab accepts
   SeaweedFS as-is, with eviction policy as the compensating lever: local eviction of
   **durable** artifacts is a space-pressure action, not routine hygiene, so NVMe keeps
   a second copy while there is room. The exposure window is then "store dies AND the
   banking node dies before re-fetch", the same double-failure class a real S3 backend
   protects against. Reconstructible artifacts need no such care (worst case is a
   re-bake or re-warm).

## Open questions

1. cpu_sku granularity: vendor+family, or pin a conservative Firecracker CPU template
   per fleet generation so snapshots stay portable across minor SKU differences at some
   feature cost. This is quietly the most important R7 question: without a fleet-wide
   template, each hardware generation fragments the snapshot pool into incompatible
   SKU islands and "move warmth" silently becomes "rebuild warmth".
2. Does Phase 0's bake cache change the rootfs versioning story (rootfs-<tag>.ext4
   naming today implies tag-keyed, not digest-keyed)?
3. Where the build queue's state lives: op-log rows (durable, replayable, consistent
   with everything else) is the presumptive answer.
