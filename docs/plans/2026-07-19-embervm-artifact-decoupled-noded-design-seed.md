# EmberVM Design Seed: Artifact-Decoupled noded (Stateless Daemon, S3-Distributed Artifacts)

**Status:** Design seed for review, not scheduled work. Extends the R7 Distribution seed
(docs/plans/2026-07-18-embervm-r7-distribution-design-seed.md) and ADR embervm/009's S3
durability seam. Written 2026-07-19 after the bazel-query base-build night surfaced the
costs of the current coupling.

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

## Phases (each independently shippable)

**Phase 0, quick wins (can ship this week, no design risk):**
- Digest-keyed rootfs bake cache: init containers become a no-op when the target ext4
  for the image digest already exists on NVMe. Kills the 4 to 5 minute tax on rolls
  where images did not change.
- preStop drain: noded finishes or cleanly aborts in-flight BuildBase work inside the
  termination grace period instead of orphaning half-built VMs.

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

## Open questions

1. ROOTFS artifact store: the S3 seam (consistent with everything else) vs GHCR OCI
   artifacts (closer to the image publish pipeline). Leaning S3 for one distribution
   path and one auth story.
2. cpu_sku granularity: vendor+family, or pin a conservative Firecracker CPU template
   per fleet generation so snapshots stay portable across minor SKU differences at some
   feature cost.
3. Builder role: same noded binary with a builder flag on the same node (simplest, one
   binary), vs a separate builder DaemonSet/Job (cleaner blast radius, more moving
   parts). Leaning same binary, separate pod, so serving rolls never kill builds.
4. Does Phase 0's bake cache change the rootfs versioning story (rootfs-<tag>.ext4
   naming today implies tag-keyed, not digest-keyed)?
5. Where the build queue's state lives: op-log rows (durable, replayable, consistent
   with everything else) is the presumptive answer.
