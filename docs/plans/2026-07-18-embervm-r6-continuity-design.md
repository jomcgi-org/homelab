# EmberVM R6 Continuity: design

**Date:** 2026-07-18
**Status:** Approved design, feeds the R6 spec+plan
**Relates to:** ADR embervm/001 (roadmap ladder), ADR embervm/003 (snapshot distribution), ADR embervm/008 (interruptible bank)

## Context

ADR 001's ladder is exhausted below its last rung: R0 Tasks through R5 Composite are all
Shipped (R4/R5 with live gates pending). The only unshipped rung, R6 Facade (kine-style
etcd shim, virtual control planes, hard multi-tenancy), was priced as "Future ADR": an
option kept cheap, not a commitment. Running virtual control planes is not a short-term
goal, so exercising that option now is the wrong move.

Meanwhile the R5 drill log exposed the platform's real recurring pain: every noded pod
roll destroys all live VMs and group bridges, banked warmth and stateful volumes live on
node-4 hostPath NVMe with zero redundancy, and deploy churn repeatedly killed the drills
themselves. Every rung so far added a capability; none added continuity.

## Decision (recorded as ADR embervm/009)

Defer the facade explicitly and extend the ladder with three new rungs.

- R6 Facade is demoted from "Future ADR" to **Recorded**, with a revival trigger: real
  demand for virtual control planes or hard multi-tenancy (for example external tenants).
- ADR 001's roadmap table is edited mechanically (row re-statused, new rows appended,
  pointer to ADR 009). Rationale lives in 009.

New ladder tail:

| Rung | Capability | First consumer | v1 invariant |
| --- | --- | --- | --- |
| R6 Continuity | Deploys and node-daemon rolls interrupt nothing they don't have to | every live workload (scratch-postgres, scratch-k8s, serving) | a routine noded/CP roll never cold-boots a stateful workload and never destroys a banked group |
| R7 Consumers | Agent-thread tier runs on EmberVM sessions | goosecracker / fc-agentd successor | bespoke fc-agentd controller retired |
| R8 Packaging | Standalone open-sourceable artifact | external readers | clean repo boundary + quickstart that boots on one machine |

## R6 Continuity scope

Four workstreams, in dependency order.

### 1. Graceful drain on noded roll

Today a noded pod restart destroys every live VM and group bridge. Add a drain protocol:
pre-stop (or control-plane-orchestrated pre-roll) banks everything bankable, then the new
noded relights or adopts on start.

**Availability contract (v1): spot-instance semantics with a 2-minute preemption bound.**
A roll gives every workload up to 2 minutes of drain notice to checkpoint/bank. The R6
invariant is "state is always durable and a routine roll never loses data", not "rolls are
seamless". A higher-availability tier (live migration, overlapping generations) is
recorded as a future rung, not v1.

- Stateful workloads bank via the ADR 008 interruptible-bank machinery.
- Composite groups bank as a unit (all-members-or-none, the existing bundle-set contract).
- Sessions bank via the R2 bank path.
- What cannot bank (mid-task VMs) gets a bounded grace window, then is destroyed as today.

### 2. Off-node durability for bundles and volumes

Builds ADR 003's unbuilt verbs (ExportBase / RestoreBase / EvictBase) and extends them to
banked bundles and stateful volumes. Today all of this is node-4 hostPath NVMe: a disk
loss is total data loss for scratch-postgres.

**Storage contract: a configurable S3-API endpoint.** Durable storage is a requirement
for flushing state off node and distributing it to other nodes for balancing. An S3-shaped
interface is the easiest short-term seam; SeaweedFS (already in-cluster for chat blobs) is
the first backend. Block devices / disk-level replication are a recorded future
optimization, not v1.

- Write-back async export (off the hot path), restore-on-miss.
- The same verbs that give redundancy are the mechanism for cross-node rebalancing, which
  is how ADR 003 framed its first honest consumer.

### 3. Resilience amplifiers from the R5 drills

The gaps that turned "one member can't boot" into "whole workload wedges until CP restart":

- Wake-worker timeouts (no `:infinity` GenServer.call chains holding the `waking` state).
- Adoption self-recovery for wedged wakes (adopt_one currently skips anything `waking`).
- Park-overflow recovery.

### 4. Entry criteria (not a workstream)

The pending R5 live gates close first: the entry-path EOF (serving Envoy 5410 to k3s
6443), gates 2 through 10, and the 48h soak. Continuity claims are unprovable on a system
whose baseline drills do not pass.

## Out of scope

- Virtual control planes, etcd facade, hard multi-tenancy (deferred, ADR 009).
- Block-device snapshot/volume replication (future optimization behind the S3 seam).
- fc-agentd migration (R7) and open-source extraction (R8).
- Cross-node scheduling policy beyond what export/restore mechanically enables (placement
  policy stays with ADR 003's "wait for empirical demand" stance).

## Deliverables

1. ADR embervm/009 + the mechanical ADR 001 table edit (same PR as the spec+plan).
2. Spec+plan doc for R6 Continuity (docs/plans, via the writing-plans flow).
