# EmberVM reference deployment (GKE hub)

The GKE hub, `homelab-hub`, is the only cluster and management plane.
The deployment-agnostic design lives in [../ARCHITECTURE.md](../ARCHITECTURE.md).
This guide describes checked-in configuration, not a live acceptance result.

## Values and placement

The production [Application](../../gke-apps/embervm/application.yaml) renders
release `embervm` into namespace `embervm`, in this order:

1. Published chart defaults.
2. [values.yaml](values.yaml), shared deployment settings.
3. [values-gke.yaml](values-gke.yaml), hub placement and production identities.

The shared file retains historical node floors and scratch settings. It is not
a deployable hub profile on its own. The final overlay clears the floors,
selects the hub brick mix and enables scratch preparation. Do not copy this
production overlay into dev: it carries production object-store, broker and
guest-callback identities. [Dev's guide](../dev/deploy/README.md) records its
separate value order and inactive presets.

Bricks run on the `ember-bricks` Spot pool with nested virtualization. Their
selector is `homelab.io/firecracker: "true"`; they tolerate
`embervm.jomcgi.dev/node=true:NoSchedule`. The serving relay separately selects
`embervm.io/serving: "true"`. The on-demand core pool must not receive that
serving label. Pool enrollment is infrastructure configuration, not a command
to label retired home nodes. See [platform architecture](../../platform/ARCHITECTURE.md#1-clusters).

Production's scratch-prep DaemonSet prepares the capped scratch mount on brick
nodes. Normal dev uses its own `dev` subtree and waits for production's mount
generation marker. Scratch is ephemeral across Spot replacement. CPU-vendor
keying still prevents restoring incompatible memory snapshots.

## Durable state and telemetry

- Production uses the `embervm_oplog` Postgres database on `monolith-pg`.
  `opLog.postgres.enabled` renders `EMBERVM_OPLOG_DSN` and suppresses the SQLite
  PVC. The password comes from a 1Password-managed Secret. Inspect references
  and environment-variable names, never the resolved DSN or Secret data.
- SQLite remains the chart-default backend. Its PVC uses `standard-rwo` on
  the hub; the standalone quickstart explicitly selects `local-path`.
  Switching backends does not migrate the journal and can start an empty one.
- Production's artifact store is GCS through `https://storage.googleapis.com`,
  bucket `h0melab-ember-bases`, with HMAC credentials delivered by the
  1Password Operator. Chart defaults disable the store with an empty endpoint.
  Normal dev also clears the bucket and disables store credentials. It is
  local-only and does not exercise GCS export or restore.
- The hub OpenTelemetry collector exports telemetry to Honeycomb. External
  traffic goes through Cloudflare. Kargo is at
  `private.jomcgi.dev/app/kargo`; the ArgoCD UI has no hub route.

## Read-only preflight

From the repository root, render the exact production value order:

```bash
helm template embervm projects/embervm/chart --namespace embervm \
  -f projects/embervm/deploy/values.yaml \
  -f projects/embervm/deploy/values-gke.yaml
```

This checks source manifests. The source chart has build-time image placeholders;
it is not an installation artifact. Kargo owns the live chart revision and the
git pin is a bootstrap floor. On the hub context, inspect the live Application:

```bash
kubectl get application embervm -n argocd -o json | jq '{
  chart: .spec.sources[0].targetRevision,
  values: .spec.sources[0].helm.valueFiles,
  destination: .spec.destination,
  sync: .status.sync.status,
  health: .status.health.status
}'
kubectl get nodes -l cloud.google.com/gke-nodepool=ember-bricks -o json | \
  jq '.items[] | {name: .metadata.name, labels: .metadata.labels, taints: .spec.taints}'
kubectl get deployments,pods,pvc -n embervm
kubectl get workloads -n embervm
kubectl get deployment embervm-embervm -n embervm -o json | \
  jq '[.spec.template.spec.containers[] | select(.name == "control-plane") |
    .env[].name | select(startswith("EMBERVM_OPLOG_"))]'
```

The production environment-name list must include `EMBERVM_OPLOG_DSN` and
exclude `EMBERVM_OPLOG_PATH`; no op-log PVC is rendered with Postgres enabled.
Application health alone does not prove the workload or store works. Management
reads include `/v1/nodes`, `/v1/usage`, and `/v1/health/store`, using existing
authorized access. These commands do not enroll nodes, trigger a rollout or
run a drill.

The [stateful generation quarantine runbook](../../../docs/runbooks/embervm-stateful-generation-quarantine.md)
covers the supported quarantine observation and recovery boundary. Recovery
and store-validation operations require their own authorization and evidence.

## Inactive store-validation preset

Issue #6193 owns the operational acceptance. The repository stage is inactive:
neither Application nor kustomization references
`../dev/deploy/values-store-validation-gke.yaml`. It is an alternate
configuration of the same `embervm-dev` release, not a second release. Render
chart defaults, dev `values.yaml`, dev `values-gke.yaml`, then the validation
preset. Never substitute production's GKE overlay.

The preset selects `h0melab-ember-bases-dev` and required references to the
`embervm-store-validation-gcs` Secret, with an empty 1Password item path.
It disarms the application retention deletion gates. Missing credentials
prevent store-using containers from starting. No bucket, credential, IAM
binding, lifecycle or budget is created by rendering this configuration.

[store-validation/README.md](store-validation/README.md) records the separately
authorized operator work: a dev-only seven-day delete lifecycle and an
alerts-only USD 15 monthly budget for the project's Cloud Storage service.
The budget covers more than this bucket and does not cap spending. The stage
defines no production lifecycle deletion policy; live production policy still
requires inspection before and after that validation.

## Warmth GC

An empty control-plane store of a kind whose S3 keys exist aborts the sweep
as possibly not rebuilt. `warmthS3Gc.allowEmptyKinds` explicitly identifies
retired kinds whose empty projection is expected. Unknown tokens fail chart
rendering and are dropped by the application.

The S3 warmth GC defaults to dry-run in code. The production values arm it
with `warmthS3Gc.enabled: "1"`. A rollback changes that value to `""` through
a reviewed GitOps change; chart publication manages the version. Its 8-hour
stateful TTL keeps the newest reference per vendor and workload for active
workloads, meaning any non-terminal instance or volume row. Older superseded
references become eligible after the grace window. Dead workload namespaces,
including their newest reference, are evicted after the TTL. `base/` remains
excluded.

Session and serving references, plus session-workspace lineages, have no
history retention guard. They are protected while the corresponding instance
is active, including attached and in-flight transition states. Banked and
parked states are not active, and their warmth is eligible after the
configured TTL once a parked session's CP `expires_at` has passed. A later
resume follows the session-expiry 410 path. Terminal states are expired,
evicted, destroyed, or failed for sessions, and evicted, destroyed, or failed
for serving. This documentation change does not alter any retention gate.
