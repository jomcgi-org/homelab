# EmberVM dev environment (homelab)

The development environment for EmberVM, isolated from production by design.
See the architecture and decision rationale in
[../../ARCHITECTURE.md](../../ARCHITECTURE.md) and
[ADR embervm/034](../../../../docs/decisions/embervm/034-conformance-harness-synthetic-actions-fault-injection.md).

## Isolation by construction

Two control planes sharing any artifact means they eat each other's data.
This dev environment is disjoint by design:

- **Own namespace**: `embervm-dev`, separate control plane and noded instances.
- **Own op-log database**: SQLite on a PVC, disjoint from production's shared
  monolith-pg Postgres instance. The chart falls back to EMBERVM_OPLOG_PATH
  when postgres.enabled is false.
- **nvmeRoot split**: dev bricks use `/var/lib/embervm/scratch/dev`, separate
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


## Isolation preflight

Run these after the first sync. #4762 names this as the exit criterion: an
unverifiable stand-up is worse than none, because "isolated" is a claim that
looks identical to "not yet broken".

**1. Production's node inventory is unchanged.** Capture before and after; the
set of node ids must be identical.

```bash
kubectl exec -n embervm deploy/embervm-embervm -- \
  curl -s -H "authorization: Bearer $TOKEN" localhost:8080/v1/nodes | jq -S '[.nodes[].node_id] | sort'
```

**2. The dev control plane sees exactly its one brick**, and it is the node-4
pinned 1gi class.

```bash
kubectl exec -n embervm-dev deploy/embervm-dev-embervm -- \
  curl -s -H "authorization: Bearer $TOKEN" localhost:8080/v1/nodes | jq '[.nodes[].node_id]'
```

**3. No dev brick appears in production's inventory, and vice versa.** The two
sets from steps 1 and 2 must be disjoint. This is the one that matters: noded
streams its full `primed_vm_ids` to whatever control plane it registers with and
adoption is additive, so a brick reachable by both would be cross-control-plane
double assignment by design, with no attacker required.

**4. Exactly one brick Deployment is live in dev, pinned to node-4.**

```bash
kubectl get deploy -n embervm-dev -o custom-columns=\
NAME:.metadata.name,REPLICAS:.spec.replicas,NODE:.spec.template.spec.nodeSelector
```

Anything with replicas above 0 that is not `...brick-1gi-node-4` is a bug.
`desiredReplicas` renders an UNPINNED brick per class and `nodeFloors` renders an
ADDITIONAL pinned one, so setting both produces two bricks, one free to schedule
onto a master. #4290 is that failure: an unpinned brick fills a master's 35GiB
scratch on any chart bump.

**5. The destroy gate is armed in dev and off in production.**

```bash
for ns in embervm embervm-dev; do
  kubectl get deploy -n $ns -o jsonpath=\
'{range .items[*].spec.template.spec.containers[*].env[?(@.name=="EMBERVM_NODE_CONFIRMED_DESTROY")]}{.value}{"\n"}{end}'
done
```

Dev must print `true`, production `false`. Per #4758 this ordering has never run
anywhere, so dev is its first execution.
