# EmberVM reference deployment (homelab)

The architecture is deployment-agnostic and lives in
[../ARCHITECTURE.md](../ARCHITECTURE.md). This file is the concrete shape
of the reference deployment in this monorepo.

## Fleet

| Node | CPU | Memory | Role |
| ---- | --- | ------ | ---- |
| node-1/2/3 | Intel Alder Lake-S, 12 vCPU each | ~15.3 GiB (~12.3 allocatable) | k3s control-plane/etcd masters; cold/CPU-rich tier (task-class, semgrep scans, bazel clones) |
| node-4 | AMD Zen4, 16 threads | 62 GiB | warm tier: banked sessions, serving, stateful volumes |

- The guest/etcd co-location clause from the architecture's deployment
  section is exercised here: the etcd masters carry task-class guests.
- Live brick mix: `desiredReplicas` 2gi 1 and 16gi 1, with per-node 2gi
  floor bricks pinned on node-1, node-2, and node-3; the 4gi and 8gi
  classes are present at zero replicas; chart clamps are min 16gi 1 and
  max 2gi 4 / 4gi 3 / 8gi 2 / 16gi 2.
- Warmth is vendor-keyed, so the Intel pool restores from intel-keyed
  bases and node-4 holds the AMD tier's; labelling a node of a new vendor
  into the pool refuses cross-vendor restores loudly rather than
  mis-placing them.
- The CP op-log shares the `monolith-pg` CNPG cluster: a second cluster
  would cost ~1Gi of requests on a fleet at 99% of memory limits on
  node-4, and the coupling is bounded because a CP outage is a
  designed-for state.
- The Kubernetes node taint is recorded but not applied.
- Platform services: SeaweedFS for the S3 store, the 1Password Operator
  for secrets, Cloudflare Tunnel for the zero-trust edge, SigNoz for
  observability.

## Operational entry points

ArgoCD and SigNoz at `private.jomcgi.dev/app/*`, `kubectl get workloads`
for definition status, `/v1/usage` for metering, and
`docs/runbooks/embervm-*.md` for break-glass procedures.

## Warmth GC operations

An empty control-plane store of a kind whose S3 keys exist aborts the sweep
as possibly not rebuilt. `warmthS3Gc.allowEmptyKinds` is the operator
statement that a class is retired and its store is legitimately empty, which
exempts that kind's branch. Unknown tokens fail chart rendering and are
dropped by the application, so the guard can only be weakened deliberately.

The S3 warmth GC is dry-run in code and may delete only the explicit
allowlist of warmth prefixes. It is armed in the reference deployment
(`warmthS3Gc.enabled: "1"`); rollback is setting `enabled` back to `""` and
bumping the chart. Its 8-hour stateful TTL keeps the newest reference per
vendor and workload for active workloads, meaning any non-terminal instance
or volume row. Older superseded references are eligible after the grace
window. Dead workload namespaces, including their newest reference, are
evicted after the TTL. `base/` remains excluded, so the current base and the
newest stateful reference for active workloads are preserved by construction.

Session and serving references, plus session-workspace lineages, have no
history retention guard. They are protected while the corresponding instance
is active, including attached and in-flight transition states. Banked and
parked states are not active, and their warmth is eligible after the
configured TTL once a parked session's CP `expires_at` has passed. A later
resume therefore follows the existing session-expiry 410 path rather than
reattaching an empty workspace. Terminal states are expired, evicted,
destroyed, or failed for sessions, and evicted, destroyed, or failed for
serving.
