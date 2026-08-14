# EmberVM dev environment (homelab)

The development environment for EmberVM, isolated from production by design.
See the architecture and decision rationale in
[../ARCHITECTURE.md](../ARCHITECTURE.md) and
[../docs/decisions/embervm/034.md](../docs/decisions/embervm/034.md).

## Isolation by construction

Two control planes sharing any artifact means they eat each other's data.
This dev environment is disjoint by design:

- **Own namespace**: `embervm-dev`, separate control plane and noded instances.
- **Own op-log database**: SQLite on a PVC, disjoint from production's shared
  monolith-pg Postgres instance. The chart falls back to EMBERVM_OPLOG_PATH
  when postgres.enabled is false.
- **nvmeRoot split**: dev bricks use `/var/lib/embervm/scratch-dev`, separate
  from production's shared node-4 NVMe scratch. Scratch is shared metal; the
  split prevents prod GC and dev artifacts from colliding.
- **S3 bucket split**: dev uses `embervm-dev` bucket (production uses `embervm`).
  Warmth GC and base retention operate on S3; separate buckets prevent interference.
- **Distinct ServiceAccount**: dev bricks carry only the dev CP's dial-home
  address. A brick that reaches both CPs is cross-CP double-assign by design,
  no attacker required.
- **Brick pinned to node-4**: dev uses the 1gi class only, replicas 1, pinned
  to node-4. NEVER the masters (node-1/2/3): #4290 shows their 35 GiB loop
  scratch fills on any chart bump.

## Fleet

Single 1gi brick on node-4, scaling disabled. Production has multiple classes
and autoscale enabled.

## Workloads

Only the sandbox workload is defined. The claude-runtime (4 GiB bases) is
explicitly excluded; a dev fork of the guest image belongs in a separate PR
when task-driven workloads need one.

## Fault injection and ordering

This is the only place the control plane's node-destroy ordering
(#4758) can run: fault injection on a dev CP is acceptable, never on shared
production infrastructure. EMBERVM_NODE_CONFIRMED_DESTROY is armed.

SpecTrace validation (#4770) is enabled (EMBERVM_SPEC_TRACE=on) to log all
state machine transitions against the TLA+ specs.

## Node enrollment

The 1gi brick is scheduled on node-4 via nodeSelector. No explicit taint is
applied; the serving relay and other standard bricks tolerate the node-local
FC labels.

## Operational entry points

kubectl get applications -n argocd | grep embervm-dev for the Application sync
status, and /v1/nodes for the control plane's node inventory (should contain
exactly the one dev brick). Production's /v1/nodes should be unchanged.
