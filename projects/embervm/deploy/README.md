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
- The FC node taint is recorded but not applied.
- Platform services: SeaweedFS for the S3 store, the 1Password Operator
  for secrets, Cloudflare Tunnel for the zero-trust edge, SigNoz for
  observability.

## Operational entry points

ArgoCD and SigNoz at `private.jomcgi.dev/app/*`, `kubectl get workloads`
for definition status, `/v1/usage` for metering, and
`docs/runbooks/embervm-*.md` for break-glass procedures.
