# ADR 011: Distribution via Longhorn Volumes, Single-Writer Fencing, and CP-Sequenced Rollouts

**Author:** Joe McGinley
**Status:** Accepted
**Created:** 2026-07-18
**Refines:** [009-roadmap-extension-continuity-before-tenancy](009-roadmap-extension-continuity-before-tenancy.md), [003-control-plane-managed-snapshot-distribution](003-control-plane-managed-snapshot-distribution.md)

---

## Context

ADR 009 decided R7 Distribution as a rung and left its shape open: placement
policy over the R6 export/restore verbs, demand-driven warming, multi-node
endpoint fan-out, with an assumed hardware prerequisite of a second
Firecracker-capable node. Since then, the /ember demo went public and deploys
froze while it was being shown, which inverts the platform's goal: deploys
should be boring precisely because the workloads survive them.

Three findings reshape the rung (full findings in the design seed,
`docs/plans/2026-07-18-embervm-r7-distribution-design-seed.md`):

1. **The fleet constraint was never KVM, it is snapshot portability.** All four
   cluster nodes have virtualization enabled and headroom. Firecracker is
   unambiguous that cross-vendor snapshot restore is unsupported, and even
   intra-vendor the restore matrix is narrow. The Intel control-plane nodes
   cannot consume AMD node-4's warmth, but they can run workloads cold.
2. **R6's custom volume export does not scale.** Shipping whole vol.img files
   per changed generation has write amplification that grows with volume size.
   Every incremental alternative we could build by hand (dm-thin plus
   thin_delta, content-hash manifests, filesystem send streams) reimplements
   what Longhorn, already deployed in this cluster, ships as a product:
   replication, snapshots, and incremental S3 backup.
3. **A second writable copy of anything requires a fence first.** The adoption
   design deliberately lets nodes outlive control-plane authority gaps, and it
   has already produced real bugs in the single-node world (orphaned primed
   VMs, stale group bridge records). With a second node holding state, a
   control plane that is briefly wrong about "which node is live" becomes a
   split-brain data-corruption risk rather than a warmth bug.

## Decision

### Heterogeneous fleet with vendor-bound warmth

noded runs on all four nodes. AMD node-4 is the warm tier; the Intel nodes are
a cold/failover tier. Warmth artifacts (memory snapshots, session banks,
bases) are keyed by CPU vendor and never cross the vendor boundary; bases are
built per vendor on the node that needs them. Volume data is fully portable
and is where durability lives. A cross-vendor stateful wake is a fresh boot
from the target vendor's base plus the mounted volume; sessions are
vendor-pinned because their memory image is their state.

This dissolves ADR 009's hardware prerequisite: distribution stops waiting for
a second AMD node because the Intel nodes are useful as exactly what they can
be, cold capacity behind a portable volume.

### Volumes move to Longhorn

Stateful volumes become Longhorn RWO volumes attached to a node (not mounted
into a pod), surfaced as host block devices and handed directly to
Firecracker. Durability tiers are StorageClasses (replicated for critical
workloads, strict-local single-replica for cheap scratch). Bank commits freeze
state as Longhorn snapshots; incremental backup to the S3 backupstore replaces
the custom vol.img export. The R6 store client survives with a narrower role:
vendor-keyed warmth artifacts only, which are immutable blobs an object store
suits better than a block layer.

Rejected: dm-thin/thin_delta and content-hash diff pipelines (reimplement
Longhorn inside privileged noded), btrfs/ZFS send (couples restore to a
filesystem), RWX or any shared-block design (Longhorn RWX is NFS with no raw
block device, and multi-writer block is corruption by design), and
snapshot-cloned PVCs for availability (clones fork identity; availability is
replicas of one volume).

### Single-writer HA: the control plane arbitrates, the fence enforces

One live instance per stateful workload, ever. The control plane is already
the single serializer of wakes, so election is free. But deciding is not
enforcing: from the control plane's chair, a dead node and a partitioned node
that is still writing are indistinguishable. Longhorn attach exclusivity
converts the placement decision into a physical fact; attaching the volume to
the new node invalidates the zombie's access, so its writes fail instead of
corrupting.

The warmth plane gets the same discipline through generation blessing: the
control plane becomes the sole issuer of volume generation numbers, and an
artifact whose generation was never blessed is quarantined, never exported.
This invariant lands before any second warm-capable node exists, because it is
trivial to enforce from day one and miserable to retrofit after a split brain.

The availability contract is unchanged from ADR 009: spot semantics with a
bounded preemption window. The RPO is the last settled generation; this is
single-writer failover, not active-active, and must never be mistaken for it.

### noded rollouts are sequenced by the control plane

Scheduler-timed rollouts cannot express what the fleet needs: pause when the
store is unhealthy, roll the cold tier first, wait for an idle-bank window.
noded moves to OnDelete semantics and the control plane becomes the rollout
controller: per node, check interlocks, drain (the R6 machinery), confirm
banked, delete the pod, verify the replacement, move on. GitOps is unchanged;
ArgoCD still delivers the template and Kubernetes still reconciles deleted
pods.

Two guardrails are part of the decision, not implementation detail: a
staleness bound that force-rolls with an alert when interlocks never go green
(condition-gated automation without a deadline becomes a silent freeze), and a
control-plane-down fallback (generation-lag alerting plus manual pod deletion
as the escape hatch).

The division of labor across all four mechanisms is the same: the control
plane owns WHEN; Kubernetes, Longhorn, and S3 own HOW.

### ADR 003's open placement question is resolved

Placement policy is vendor-aware preference over the R6 verbs: prefer the node
already holding vendor-matching warmth and the attached volume, then a node
with a volume replica and a vendor-matching base, then any eligible node cold.
A placement move is a copy, never a rebuild; demand-driven pre-warm is the
same restore verb with an eager trigger. Serving fan-out remains
single-instance in this rung; N-way read fan-out is application-level
replication, parked.

## Consequences

What becomes possible:

- Deploys stop being events. Rolls land in idle windows the control plane can
  see, cold tier first, and a mid-roll visitor wake lands on another node and
  answers slowly once instead of appearing dead.
- Node death stops being an outage for replicated workloads. Failover is a
  fresh attach plus a cold boot, bounded in seconds, with the RPO at the last
  settled generation.
- The custom storage surface shrinks. Replication, snapshots, and incremental
  backup are consumed as Longhorn features instead of built inside noded.
- Live migration acquires a door: migratable dual-attach handover is the
  storage half of the zero-interruption tier ADR 009 deferred; only
  Firecracker memory transfer would remain.

What is given up:

- Warm capacity does not multiply until a second AMD-class node exists; the
  Intel tier is cold by physics (snapshot portability), not by policy.
- Per-volume Longhorn engine overhead bounds "every workload gets a volume";
  an admission ceiling is recorded before that is ever proposed.
- The live volume's writes since the last bank commit remain node-local unless
  a workload explicitly adopts a replicated live volume (the recorded
  "airlock" option), which must be measured against the demo's latency numbers
  before anyone adopts it.

What stays true:

- The hit/miss invariant: attach, blessing, and rollout actions are lifecycle
  actions; the request hot path is untouched.
- Spot semantics (ADR 009): durability is the hard guarantee, connections are
  not.
- Fail closed on enforcement (fence conflicts, unblessed generations,
  vendor-mismatched restores), fail open on warmth (a missing store degrades
  to cold boot, never to refusing local-state wakes).

The exploratory rung recorded in the design seed (right-sized multi-pod noded,
control-plane binpacking with in-place pod resize, forecast-driven pre-warm)
is future work on top of these mechanisms and needs its own decision record
before commitment.

## Amendment (2026-07-22): the abort lane is a blessed generation issuer

The "control plane is the sole issuer of a volume generation" rule (standing
decision 4) originally covered only the wake/attach path. The ADR-008
interruptible-bank ABORT path was missed: it resumes a paused guest (which may
write), so it must advance the generation, but it did so with the legacy
node-side self-bump. A self-bump reads `generation_blessed:false` and, being past
the last blessed value, quarantined the volume on the next report, which
fail-closed every subsequent wake. This surfaced as a `demo-postgres` outage
(`jomcgi.dev/health` 503) after a normal checkpoint-abort.

The rule now extends to the abort lane: when the control plane decides ABORT it
blesses the next generation (op-log-before-dispatch, the same fence as a wake) and
threads it into `ResolveStateful`; noded records it as blessed rather than
self-bumping. The one remaining self-bump is noded's own resolve-timeout
auto-abort, where no control plane is reachable to issue a generation; its
resulting quarantine is accepted as correct fail-closed behaviour, with a
break-glass recovery documented in
`docs/runbooks/embervm-stateful-generation-quarantine.md`. See
`docs/plans/2026-07-22-embervm-abort-generation-blessing.md` for the full design.
