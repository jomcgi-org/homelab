# ADR 031: Control-Plane / Data-Plane Split for the Agent Substrate (cluster + node)

**Author:** jomcgi
**Status:** Accepted
**Created:** 2026-07-01
**Builds on:** [030 - fc-invoke Configurable Firecracker Surface](030-fc-invoke-configurable-firecracker-surface.md) (the daemon this splits), [019 - Substrate Executor AgentWorkflow](019-substrate-executor-agentworkflow.md) (the `Substrate` executor seam this brackets from the other side), [022 - Firecracker Snapshot/Restore Controller](022-firecracker-snapshot-restore-controller.md) (the node-affine microVM lifecycle)

---

## Problem

`fc-invoke` (ADR 030) is today a single node-affine daemon: it runs on node-4, holds the workload table, and drives Firecracker microVMs locally, serving `POST /invoke/{workload}`. Every part of the Go code under `projects/firecracker/substrate/` is node data-plane: the FC driver, the per-workload invoker, the vsock transport, the egress forwarder. There is no cluster-level component; placement is implicit ("this node, these workloads").

The near-certain next step is a **central agent** that fans work out across many node daemons: horizontal scaling of the invoke surface, cross-node placement, and fleet management (draining, capacity, and in-place pod resize of the node daemons under load). If that central role grows inside the same undifferentiated package tree, the node hot path (driving microVMs) and the cluster concerns (routing, scaling) entangle, and the eventual physical split becomes a rewrite constrained by a wire protocol rather than a refactor behind a Go interface.

The cheapest moment to draw the control-plane / data-plane line is now, before a second node exists and while the boundary is still an in-process interface call we can move freely.

## Decision

Split the substrate Go code into two role-scoped groups with a neutral seam between them, kept as **one binary and one Deployment for now**.

1. **`cluster/` is the control plane** (central, horizontally scalable later): request ingress, the workload catalog, and, in future, placement and fleet management. Today it is `ingress` (the HTTP front that routes `/invoke/{workload}` to a node executor) and `catalog` (the workload table loader). Placement is currently the trivial workload-to-local-executor map inside ingress; a `placement` package and a `fleet` package (scaling, in-place pod resize, capacity) slot in here without touching the data plane.

2. **`node/` is the data plane** (on-node, one daemon per node eventually): everything that must run where the microVM runs. `invoker` (per-workload FC lifecycle), `fcvm` (the Firecracker driver + API client), `vsockhttp` (the HTTP-over-vsock transport), and `egress` (the vsock-to-sidecar forwarder).

3. **`substrate/` holds the neutral seam** both planes depend on: the `NodeExecutor` interface (`Invoke(ctx, session, body) -> *http.Response`, plus the `GuestUnavailable` error convention that maps a failed claim to 503), and the `Workload` spec type. The dependency graph is `cluster -> substrate <- node`: neither plane imports the other. This brackets the node from the other side of the ADR-019 `Substrate` executor seam (consumers -> executor there; control plane -> executor here).

4. **One process wires them today.** `cmd` (the `fc-invoke` binary + image) is the only package that imports both planes: it loads the catalog, builds one `node/invoker` (a `substrate.NodeExecutor`) per workload, and hands the map to `cluster/ingress`. The seam is satisfied by an in-process local executor, a direct call. The physical split later is additive: a `remoteNode` client that dials a node daemon over gRPC/HTTP is just another `NodeExecutor`, and `cmd/cluster-agent` + `cmd/node-daemon` binaries wire the same packages across the network. No package moves, no interface change.

This is a pure move-and-rewire: no behavior change, no deploy change, one binary, one chart.

## Consequences

**Positive.**

- The control/data-plane boundary is a real Go interface today, refactorable at zero wire-compatibility cost, and the physical split (central agent + per-node DaemonSet) becomes a wiring change rather than a rewrite.
- Future optimizations land where they belong without risking the microVM hot path: **in-place pod resize** and horizontal scaling are `cluster/fleet` concerns; the node data plane never depends on them.
- "Central agent distributes to N node daemons" reduces to `placement` returning a `remoteNode` executor instead of the local one.

**Costs and risks.**

- A one-time mechanical churn: package moves, import-path rewrites, and BUILD regeneration across the substrate tree. The guest side (`shim`, `vsockproto`) and the `egress-proxy` sidecar are untouched, and there is no runtime change, so the blast radius is compile-time only and CI (build + gazelle + the existing daemon tests) is the verifier.
- A structure that anticipates a second node while only one exists is mild speculative generality; it is justified because the seam is cheap now and expensive later, and it is the owner's stated direction.

## Alternatives considered

- **Two binaries now, one Deployment** (agent -> daemon over localhost or as two containers). Rejected for now: it proves the network boundary early but adds a hop plus serialization for zero benefit on a single node, and doubles the ops surface. The interface seam gives the same future-proofing without the runtime cost.
- **Full physical split now** (a cluster Deployment with an HPA plus a node DaemonSet over the network). Rejected as premature: maximum future-alignment at maximum cost for a workload that runs on one node today. It remains the target once there is real multi-node demand; this ADR is exactly the structure that makes it a wiring change.
- **Leave it undifferentiated and split when needed.** Rejected: the split is far cheaper to draw before a second node and before a wire protocol constrains the boundary; deferring it trades a small refactor now for a large one under load later.

## Future work

- A `cluster/placement` package once there is more than one node (today the ingress map is the placement table).
- A `cluster/fleet` package for capacity, draining, and Kubernetes in-place pod resize (KEP-1287) of node daemons under observed load.
- `cmd/cluster-agent` and `cmd/node-daemon` binaries plus a `remoteNode` `NodeExecutor` (gRPC/HTTP) when the physical split is warranted, with the node daemon becoming a DaemonSet and the cluster agent a horizontally scaled Deployment.
