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
- Live brick mix: `desiredReplicas` 2gi 1 and 16gi 1, plus per-node 2gi
  floor bricks pinned on node-1, node-2, node-3 and a second 16gi brick
  pinned on node-4 (doubles session admission headroom and keeps one 16gi
  brick up through every roll); the 4gi and 8gi classes are at zero
  replicas; chart clamps are min 16gi 1 and max 2gi 4 / 4gi 3 / 8gi 2 /
  16gi 2.
- Warmth is vendor-keyed, so the Intel pool restores from intel-keyed
  bases and node-4 holds the AMD tier's; labelling a node of a new vendor
  into the pool refuses cross-vendor restores loudly rather than
  mis-placing them.
- The reference values configure the CP op-log on the `monolith-pg` CNPG
  cluster (`opLog.postgres.enabled: true` in `values.yaml`). A second cluster
  would cost ~1Gi of requests on a fleet at 99% of memory limits on node-4,
  and the coupling is bounded because a CP outage is a designed-for state.
  At runtime, `Embervm.Application.op_log_mod/0` selects Postgres only when
  the rendered pod has a non-empty `EMBERVM_OPLOG_DSN`; the verification
  command is documented in
  [../../monolith/deploy/embervm-oplog-secret.md](../../monolith/deploy/embervm-oplog-secret.md#verifying-which-backend-is-live).
- SQLite-WAL remains the chart-default, zero-dependency backend. The isolated
  dev deployment uses it on a PVC, and the reference values retain the SQLite
  size and storage settings for a backend flip that starts with an empty
  op-log. These configured roles are defined in `../chart/values.yaml`,
  `../dev/deploy/values.yaml`, and this directory's `values.yaml`; they do not
  decide SQLite's future.
- The Kubernetes node taint is recorded but not applied.
- Platform services: SeaweedFS for the S3 store, the 1Password Operator
  for secrets, Cloudflare Tunnel for the zero-trust edge, SigNoz for
  observability.

## Node enrollment

noded runs on every brick; bricks schedule onto Kubernetes nodes carrying
the enrollment label. Each daemon dials home: on start and on a jittered
interval it POSTs its identity to `/v1/nodes/register`, and the control
plane adopts it keyed by `(node, pod_uid)`. A node label is node-lifecycle
configuration, the same class as joining the node, so growing the fleet is
a label, not a values edit:

```bash
kubectl label nodes node-1 node-2 node-3 homelab.io/firecracker=true
```

Serving-capable nodes also carry the serving label so the relay schedules
there:

```bash
kubectl label nodes node-1 node-2 node-3 embervm.io/serving=true
```

Decided direction (#4696): both labels consolidate on one
chart-configurable key, `embervm.jomcgi.dev/node`, with serving implied.
Never remove the firecracker label from an enrolled node before every
consumer of it has migrated; the label is overloaded beyond noded.

Before labelling a node:

- Bind-mount its real scratch device at `/var/lib/embervm/scratch` (fstab
  entry or a systemd mount unit); the hostPath fails closed if the mount
  is missing. Use a separate disk from the etcd WAL disk.
- Expect vendor keying: memory snapshots never cross the AMD/Intel
  boundary, so a node of a new CPU vendor refuses cross-vendor restores
  loudly until its own warmth builds. This is the fail-closed gate, not a
  fault.

The hard node taint (`embervm.jomcgi.dev/node=true:NoSchedule`) stays a
recorded option, not applied. If a node ever needs it, confirm the chart
tolerations are live first: tolerations first, taint second, never the
reverse.

## Operational entry points

ArgoCD and SigNoz at `private.jomcgi.dev/app/*`, `kubectl get workloads`
for definition status, `/v1/usage` for metering, and
`docs/runbooks/embervm-*.md` for break-glass procedures.

## GKE store validation stage

Issue #6193 has a repository-only, default-off validation stage. The live GKE
Application remains configured for `h0melab-ember-bases` in `values-gke.yaml`.
The inactive
`../dev/deploy/values-store-validation-gke.yaml` preset instead fixes every
rendered store consumer to `h0melab-ember-bases-dev`, but neither Application
nor kustomization references it. It cannot change live routing automatically.

The preset enables required Secret references to
`embervm-store-validation-gcs` and deliberately leaves the 1Password item path
empty. A missing Secret therefore prevents store-using containers from
starting, while the repository does not guess an external credential path. It
also overrides the base dev values to disarm all application-level retention
delete gates, leaving the separately applied seven-day GCS lifecycle as the
only intended deletion policy for the validation bucket.

The checked-in desired policies and the inspect-before-apply operator steps are
in [store-validation/README.md](store-validation/README.md). They specify a
dev-only delete lifecycle at age seven days and an alerts-only USD 15 monthly
budget filtered to the `h0melab` project and Cloud Storage service resource
`services/95FF-2EF5-5EA1`. The budget covers all project Cloud Storage usage,
not only one bucket. Alerts do not cap spending. No bucket, lifecycle, budget,
notification channel, credential, or IAM resource is created by this repository
stage. It defines no production lifecycle deletion rule. An operator must still
verify the live production bucket has none before and after validation.

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
