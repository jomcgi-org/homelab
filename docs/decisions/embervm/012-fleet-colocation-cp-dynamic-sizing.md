# ADR 012: Fleet Co-Location on the etcd Masters and CP-Managed Dynamic Sizing

**Author:** Joe McGinley
**Status:** Accepted
**Created:** 2026-07-19
**Refines:** [009-roadmap-extension-continuity-before-tenancy](009-roadmap-extension-continuity-before-tenancy.md), [011-distribution-longhorn-fencing-cp-rollouts](011-distribution-longhorn-fencing-cp-rollouts.md)

---

## Context

ADR 011 established the heterogeneous fleet (noded on all four nodes,
vendor-bound warmth) and the artifact-decoupling design seed
(docs/plans/2026-07-19-embervm-artifact-decoupled-noded-design-seed.md, PR-A
of its implementation plan already shipped) made noded's boot independent of
the artifact lifecycle. What neither decided is the concrete fleet shape, how
guest memory becomes visible to the Kubernetes scheduler when Firecracker VMs
share nodes with platform workloads, and how the second vendor pool is keyed
without stranding the artifacts already cut.

The capacity picture forced the question. node-4, the sole Firecracker node,
is the AMD Zen4 worker (family 25 model 97, 62GiB, 16 vCPU) and is
CPU-request-saturated at 93%. The other three nodes, node-1/2/3, are the k3s
control-plane/etcd masters: identical Intel Alder Lake-S parts (family 6
model 151, ~15.3GiB RAM / ~12.3GiB allocatable, 12 vCPU each), each sitting
on roughly 4 to 6GiB of free memory and ~8 idle cores. The masters are
CPU-rich exactly where node-4 is CPU-tight. No new hardware is planned for a
while; the fleet is what already exists.

## Decision

### The fleet is the four existing nodes

Firecracker guests co-locate on the three etcd masters alongside the k3s
control plane, joining node-4. No hardware purchase gates any rung. The
masters contribute complementary capacity: abundant CPU and modest memory,
the inverse of node-4's profile, so CPU-heavy short-lived guests (task-class
sandbox, semgrep, bazel-query clones) fit the masters while memory-heavy
long-lived state stays biased toward node-4.

### etcd blast radius is explicitly accepted

Running untrusted-code microVMs on the quorum nodes is a deliberate,
eyes-open risk acceptance, not an oversight. The rationale: this cluster is
GitOps-reconstructible end to end, and all durable task state lives in S3
(ADR 009's durability seam, made uniform by the artifact-decoupling work).
An etcd or quorum loss is therefore a reboot or a rebuild, not data loss;
reboots have cleared every real incident of this class so far. etcd is not
precious here.

What this trades away is stated plainly: an availability-focused production
cluster would never co-locate guest memory pressure with its quorum, because
there a control-plane outage is an outage. This homelab can accept it because
its recovery model is rebuild-from-Git plus restore-from-S3, so the worst
case is bounded downtime, not lost state. Guests remain the kernel's first
OOM victims (guestOomScoreAdj 1000) and run in the disposable priority tier,
which bounds, but does not eliminate, the pressure a runaway can put on a
master. Anyone importing this design into a cluster whose etcd is precious
must not import this clause.

### Guest capacity becomes scheduler-visible: cgroup-bounded guests plus CP-owned in-place resize

Firecracker VMs are noded's child processes and live inside noded's pod
cgroup, so the pod's resource envelope already bounds them physically. What
was missing is the scheduler's view: a fixed noded request with a huge limit
(the current 256Mi request / 36Gi ceiling) makes guest memory invisible to
bin-packing, which was tolerable on a dedicated node and is not tolerable on
shared masters.

The control plane closes that gap with Kubernetes in-place pod resize
(InPlacePodVerticalScaling, the pods/resize subresource, stable in the
cluster's 1.35): a CP resize control loop adjusts each noded pod's memory and
CPU request AND limit to reflect the guest capacity it has provisioned or
committed on that node. Guest memory is thereby honest in the scheduler's
ledger. The policy is grow-eager / shrink-lazy: grow aggressively ahead of
placement (an in-place grow is cheap and safe), shrink reluctantly (lowering
memory in place may require a pod restart, which is exactly the disruption
this platform exists to avoid). A resize the kubelet cannot satisfy is a
placement refusal: the control plane treats resize-not-satisfiable as
"this node cannot take the workload" and places elsewhere, rather than
overcommitting.

This replaces fixed static sizing (maxLiveVMs as a capacity model, the
hand-computed 36Gi ceiling arithmetic) with a live capacity ledger owned by
the thing that already owns placement. New machinery this commits to: the
resize control loop, pods/resize RBAC for the control plane, and the
refusal-on-unsatisfiable policy.

Because guest memory is scheduler-visible, **no hard FC taint is needed**.
All four nodes are labeled as FC nodes and the scheduler bin-packs guests and
platform workloads together on honest requests, with the disposable priority
class as the pressure valve. The taint machinery PR-A shipped is simply never
applied: the tolerations already in the chart are harmless no-ops, and the
README's taint runbook becomes a recorded option rather than a step. Taint
semantics (exclusive nodes) would waste the masters' whole point, which is
shared capacity.

### Two vendor snapshot pools, and a grandfather rule that prevents data loss

The CPU vendor split is real and permanent: snapshots restore only within a
vendor (ADR 011), so the fleet has exactly two pools, Intel (node-1/2/3, one
shared template, the seed's resolved question 9) and AMD (node-4). cpu_sku
stays `(vendor, template)` with a conservative per-vendor Firecracker CPU
template; the Intel/AMD line is uncrossable for restore and no template
changes that.

The grandfather rule is the critical clause: **unstamped legacy durable
artifacts (session banks and stateful generations cut before cpu_sku
stamping existed) stay node-pinned and restorable where they were cut.** A
restore does not need the template (the vCPU state is already in the
snapshot), so the mismatch gate must NEVER refuse a legacy artifact on the
node that created it; such artifacts are simply never distributed cross-node.
Without this rule, turning on the gate would strand every pre-existing bank,
which is data loss by policy. Fail loudly on a genuine mismatch, fail open on
the artifact's home node.

One verification is part of the decision, not implementation detail: the
chosen conservative template must be proven to boot and snapshot on the real
silicon before the key is hard-coded. Alder Lake-S is a hybrid P/E-core
design, so confirm the masters present a homogeneous topology to guests
(6P+0E-style) and that the template masks cleanly there, and confirm the AMD
template on Zen4. A template key that never booted on its own pool is a
liability with a version number.

### Storage: the seed's three tiers, confirmed and sharpened

The seed's tier taxonomy stands. The clarifications this ADR records:

- **Reconstructible cache is node-local hostPath, deliberately.** noded is a
  privileged node hypervisor daemon, the same class as a CSI/CNI/device
  plugin, and gets the node-agent hostPath exception: HA lives at the
  S3-plus-placement layer, not the storage-mount layer, and untrusted code is
  isolated by Firecracker, not by avoiding hostPath. Two alternatives are
  rejected explicitly. Per-pod ephemeral storage breaks the surge warm
  handover and converts the retired bake tax into a fetch tax, coupling every
  roll to S3 uptime. Longhorn-replica-1 keeps the engine/attach/CSI machinery
  while disabling replication (the only benefit, which S3 already provides),
  its RWO semantics break the shared surge cache, and the attach lifecycle
  adds a boot-blocking failure surface in front of data that is by definition
  reconstructible.
- **Longhorn remains only for tier 3**, the live pg volumes, where
  attach-as-fence earns its keep (ADR 011).
- **Durable-tier eviction gains re-HEAD verification**: before deleting a
  local durable artifact, re-confirm the S3 object still exists at eviction
  time rather than trusting a stale export record. A confirmed-then-deleted
  export is the one path by which "evict only after export confirmed" could
  still lose data; a HEAD at evict time closes it.

### Rolls are rare, and cheap when they happen

Adding or changing a workload or function does not roll noded. The control
plane builds it out of band (the build queue), registers it (the pushed
registry), and distributes it (S3). noded rolls only on a change to its own
binary or image, which is rare. When a roll does happen it is fast or
zero-downtime: surge semantics (maxSurge 1 / maxUnavailable 0),
instance-id-partitioned state so two daemons coexist on one node, and
readiness meaning "registry replayed and adopted". The operational posture
this buys: the fleet's steady state is boring, and the exceptional event is
engineered rather than endured.

### Control-plane-managed deployments, with a DaemonSet bridge

The target is unchanged from the seed: the control plane authors one noded
Deployment per registered node and owns roll choreography; the chart's static
noded Deployment is deleted. Until that lands, the interim bridge is a
DaemonSet over the FC-labeled nodes, which gets noded onto all four nodes
now. The bridge accepts plainer rolling-update semantics in the interim; the
single node-pinned Deployment cannot reach the masters and a per-node Helm
templating layer was already rejected (one customer, deleted when the
controller arrives). The control plane owns workload and artifact placement
via the pushed registry the entire time; the bridge only changes who stamps
the pod, not who decides what runs.

### HA

The embervm control plane runs multi-replica (the sharded-CP op-log design,
ADR 007, is the relevant prior art for splitting its state). The fleet gives
serving and session workloads node-level redundancy for the first time, and
drain plus surge keep rolls non-disruptive, so neither a node loss nor a roll
is an event a caller must notice beyond the bounded preemption contract
(ADR 009).

### Registry survives a restart: never warm-to-dead

noded persists its last-synced registry to NVMe, marked stale. A restarting
noded whose control plane is briefly down serves warm workloads from its
cache using the stale registry instead of refusing everything; SyncRegistry
reconverges on reconnect and the stale mark clears. This extends the store's
degraded-mode invariant (a warm node keeps serving when S3 blinks) to the
control plane itself: no dependency's brief absence may turn a warm node into
a dead one.

## Consequences

What becomes possible:

- Roughly 12 to 18GiB of guest memory and ~24 idle cores join the fleet with
  zero hardware spend, and the Intel pool makes ADR 011's cold tier warm for
  Intel-cut artifacts.
- The scheduler's view of every node is honest, so guests, platform pods, and
  the k3s control plane share nodes on real numbers instead of taints and
  folklore arithmetic.
- Serving and session workloads get node-level redundancy; a node loss
  becomes a placement event.
- Capacity planning becomes a control-plane policy loop instead of a
  values-file calculation revisited by hand.

What is given up:

- Quorum isolation. A guest-driven memory or IO storm on a master can degrade
  etcd; the accepted worst case is a node reboot or cluster rebuild, bounded
  by the GitOps-plus-S3 recovery model. This is the headline trade and it is
  accepted eyes-open, not incidentally.
- Exclusive-node simplicity. Without a taint, correctness of co-existence
  rests on the resize loop keeping requests honest; a bug there overcommits a
  master rather than being caught by a scheduling fence.
- In-place shrink is not free. Memory shrink may require a restart, so
  released capacity is reclaimed lazily; the ledger can run temporarily fat.
- Legacy durable artifacts never gain mobility. Grandfathered banks stay
  pinned to their cutting node for life; only newly stamped artifacts
  participate in distribution.

What stays true:

- The hit/miss invariant (ADR 009): resize, registry push, export, restore
  are lifecycle actions; the request hot path is untouched.
- Vendor-bound warmth and single-writer fencing (ADR 011) are unchanged;
  this ADR adds nodes to the pools, not exceptions to the rules.
- Fail closed on enforcement, fail open on warmth: a cpu_sku mismatch is
  refused loudly, except that a legacy artifact on its home node is always
  restorable; a missing store or control plane degrades to serving what is
  local, never to refusing it.
- Git remains the source of truth for versions; the control plane is the
  actuator of rolls, not the author of desired state.
