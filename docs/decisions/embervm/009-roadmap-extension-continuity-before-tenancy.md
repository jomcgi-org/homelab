# ADR 009: Roadmap Extension, Continuity Before Tenancy

**Author:** Joe McGinley
**Status:** Accepted
**Created:** 2026-07-18
**Refines:** [001-embervm-beam-firecracker-workload-orchestrator](001-embervm-beam-firecracker-workload-orchestrator.md)

---

## Context

ADR 001's roadmap ladder is exhausted below its last rung. R0 Tasks through R5
Composite are all Shipped. The only unshipped rung, R6 Facade (a kine-style etcd
shim, virtual control planes, hard multi-tenancy), was recorded as "Future ADR":
an option kept cheap by the op-log plus ETS state model, not a commitment to
build. Running virtual control planes is not a short-term goal, so exercising that
option now would be building capability nobody is waiting on.

The R5 live drills exposed where the platform's real gap is, and it is not
capability. Every noded pod roll destroys all live VMs and every group bridge.
Banked warmth and stateful volumes live on single-node hostPath NVMe (node-4) with
no redundancy, so a disk loss is total data loss for scratch-postgres. Deploy churn
repeatedly killed the drills themselves. Every rung so far added a workload class;
none of them made a routine roll survivable. Capability rungs are done. Continuity
was never a rung.

## Decision

### R6 Facade is demoted to Recorded

The facade drops from "Future ADR" to Recorded. It stays reachable (the op-log plus
ETS shape that backs it is unchanged) but carries no commitment. The revival trigger
is real demand for virtual control planes or hard multi-tenancy, for example
external tenants who need an isolated control-plane view. Absent that demand, the
facade is not built.

### The ladder gains four rungs

_(Amended 2026-07-18, same day: the original three-rung extension gained R7
Distribution, and Consumers/Packaging moved down to R8/R9. R6 makes state portable
and durable; a distinct rung is needed to make the fleet actually use that
portability.)_

- **R6 Continuity.** Deploys and node-daemon rolls interrupt nothing they do not
  have to. The first consumer is every live workload already running: scratch-postgres,
  scratch-k8s, and serving. The v1 invariant is that a routine noded or control-plane
  roll never cold-boots a stateful workload and never destroys a banked group. R6
  makes state portable and durable where the architecture permits: portability is
  bounded by ISA/CPU compatibility (a Firecracker snapshot restores only on a
  matching CPU key; the current fleet has one AMD Firecracker node, and the Intel
  control-plane nodes are not microVM hosts) and by volume anchoring (a live
  stateful workload's writes since its last bank commit exist only on its node's
  volume). Within those bounds, every banked artifact is durable off node and
  movable as a copy.
- **R7 Distribution.** Redistribute and pre-warm workloads across many nodes where
  necessary: placement policy over the R6 export/restore verbs (a placement move is
  a copy, never a rebuild), demand-driven warming (restore an artifact onto a node
  before the request that needs it), and multi-node endpoint fan-out for the serving
  class. This rung resolves ADR 003's open placement-policy question and carries an
  explicit hardware prerequisite: at least one additional Firecracker-capable node,
  since distribution across a fleet of one is vacuous.
- **R8 Consumers.** The agent-thread tier runs on EmberVM sessions, which retires the
  bespoke fc-agentd controller. The durability work in R6 makes session state
  node-loss-tolerant first, which is the property that tier was missing.
- **R9 Packaging.** EmberVM becomes a standalone, open-sourceable artifact: a clean
  repo boundary and a quickstart that boots on one machine.

### The availability contract: bounded preemption, not seamless rolls

R6 continuity v1 is spot-instance semantics with a two-minute preemption bound. A
roll gives every workload up to two minutes of drain notice to checkpoint and bank,
and workloads that cannot finish in that window are torn down and re-woken against the
new daemon. This deliberately softens R4's shipped invariant, "a long-lived connection
is never severed," to "never severed except by preemption, with a two-minute bound."

The trade is explicit. State durability is the hard guarantee: a routine roll never
loses committed data. Connection continuity is not: a parked or live caller may be
dropped by a roll and must re-wake, which the scratch-* consumers already tolerate
through retries. A higher-availability tier (live migration, overlapping daemon
generations, zero-interruption rolls) is recorded as future work, not part of v1.

### The durability seam: a configurable S3-API object store

Off-node durability and cross-node rebalancing ride a configurable S3-API object-store
endpoint. SeaweedFS (already in-cluster for chat blobs) is the first backend. Banked
bundles, bundle sets, and stateful volume generations export off node as async
write-back after their bank commits, and wakes restore from the store on a local miss.
Block-device and disk-level replication are recorded as a future optimization behind
the same seam, not v1.

This is also ADR 003's first honest consumer. ADR 003 defined base-snapshot
export, restore, and eviction verbs against an object store (leaving the store
choice open) but shipped nothing that used them. R6 generalizes those verbs from bases to every artifact kind (sessions,
serving, stateful bundles, group sets, volumes) and puts them on a real durability and
rebalancing path.

### R6 continuity implementation decisions (recorded)

These standing decisions were made while implementing R6 and are recorded here so
the rationale survives (they were previously kept only in an implementation log that
has since been folded into GitHub issues and removed):

- **Drain holds the whole gRPC surface up; noded does not GracefulStop early.** On
  SIGTERM the node keeps serving lifecycle RPCs (only new BuildBase/Prime/Assign are
  refused via a draining flag) and waits on a managed-drain barrier until the
  session/serving/stateful/group registry empties or the deadline, then GracefulStop
  drains in-flight Assigns. The pre-R6 immediate GracefulStop rejected the control
  plane's own Bank/Stop calls, which is exactly what a clean drain needs. The barrier
  wakes on the existing NodeStatus change broadcast plus a 500ms backstop ticker and
  the deadline timer, not a fake clock.
- **All-classes force-bank on drain, encapsulated per sweeper.** A thin DrainCoordinator
  fans out to a per-class `drain_node/2` that routes each class's live instances through
  its existing bank machinery. Stateful drain force-banks unconditionally (a small
  `draining_workloads` set flips the raced/scrape-fail/at-cap aborts to commit even
  against a parked connection) and bypasses the per-node bank cap, because a drain
  evacuates every instance and deferring at-cap would strand them; the 120s deadline and
  the daemon hold bound concurrency instead. Serving and session drains keep their caps.
- **Async object-store exports never block the bank path or the drain deadline.** Exports
  are a fire-and-forget bounded worker pool; an enqueue that would block is dropped, and a
  startup reconcile sweep re-enqueues any artifact whose store copy is missing or stale.
  This is the hard rule that keeps durability write-back off the latency-critical path.
- **Restore-on-miss is optimistic for warmth, fail-closed for data.** Bundle and set
  restores (pure warmth) are attempted on any local miss whenever the store is reachable
  and degrade to a logged cold boot when absent. A volume restore is a data action, so it
  stays gated on the durable `exported_generation` and never blindly restores. An
  unreachable store never blocks a local-state wake; only a true local miss consults the
  store.
- **The four continuity alerts ship disabled (dry-run) by deliberate posture.** No
  op-log/log to metrics bridge exists yet, so an enabled alert would query a non-existent
  metric. A disabled placeholder is the honest posture (no fake-but-passing query firing
  silently) and matches the existing embervm alert convention; the alerts are promoted
  during the live closure drills. Outstanding closure work is tracked in GitHub issues,
  not here.

## Consequences

What becomes possible:

- Routine rolls stop being destructive. A noded or control-plane roll banks live state
  within the preemption window and relights it afterward, instead of cold-booting
  everything.
- Node disk loss stops being data loss. State that has exported off node survives the
  loss of the node it was banked on.
- Cross-node rebalancing becomes mechanically cheap. The same export and restore verbs
  that give redundancy move an artifact between nodes, so placement changes are a copy,
  not a rebuild.

What is given up:

- No seamless rolls in v1. Within the two-minute bound a caller can be dropped and must
  re-wake. The seamless tier is deferred.
- Facade work is deferred. Virtual control planes and hard multi-tenancy wait for real
  demand.

What stays true:

- The hit/miss invariant holds. Exports, restores, and drain actions are lifecycle
  actions; the request hot path is untouched.
- The isolation rules hold. Store keys are namespaced by workload, no artifact is
  restored into another workload's lineage, and there is no cross-principal sharing.
