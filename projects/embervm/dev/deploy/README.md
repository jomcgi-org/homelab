# EmberVM dev on the GKE hub

`embervm-dev` is the conformance lane on `homelab-hub`. Kargo promotes a chart
through this release before production. The design is in
[../../ARCHITECTURE.md](../../ARCHITECTURE.md).

## The Application defines the value order

The [hub Application](../../../gke-apps/embervm-dev/application.yaml) uses release
and namespace `embervm-dev`, with chart defaults followed by
[values.yaml](values.yaml), then [values-gke.yaml](values-gke.yaml).
The shared dev file retains historical home placement settings, so it must
not be used alone on the hub. Production's `../../deploy/values-gke.yaml`
must never enter this stack: its store, broker and guest callbacks identify
production.

Normal dev configures:

- A SQLite op-log PVC using `standard-rwo`, separate from production's Postgres
  database. `EMBERVM_OPLOG_PATH` is present and `EMBERVM_OPLOG_DSN` is absent.
- No object store: endpoint and bucket are empty and store credentials are
  disabled. Session bank/relight stays node-local; it proves no GCS recovery.
- Scratch and rootfs paths under `/var/lib/embervm/scratch/dev`. The shared
  scratch gate waits for production's prepared mount and generation marker,
  then mounts the dev subdirectory. Dev does not run another scratch-prep.
- Release-scoped ServiceAccounts, namespace-scoped control-plane resource
  access, and dev dial-home addresses. TokenReview and node reads retain
  their necessary cluster scope.
- One bootstrap `2gi` brick on the shared `ember-bricks` Spot pool. There is no
  hostname pin. The selector is `homelab.io/firecracker: "true"`, with the
  `embervm.jomcgi.dev/node=true:NoSchedule` toleration. A bounded `4gi` canary
  can open on demand under the current full-mode autoscaler. Zero rendered
  replicas alone do not describe the controller's eventual target.
- The `sandbox-python`, `pi-runtime`, and `sandbox-elixir` workloads plus the
  conformance runner. Normal dev does not define `claude-runtime`.

Node-confirmed destroy ordering is enabled in both production and normal dev.
The original dev drill and production arming are recorded under closed #4758;
this guide does not claim a new drill. SpecTrace remains enabled in dev.

## Read-only isolation preflight

From the repository root, render the same value order the Application uses:

```bash
helm template embervm-dev projects/embervm/chart --namespace embervm-dev \
  -f projects/embervm/dev/deploy/values.yaml \
  -f projects/embervm/dev/deploy/values-gke.yaml
```

Inspect the rendered PVC, workload names, rootfs builder paths, scratch mounts,
broker references and each noded `EMBERVM_NODED_CONTROL_PLANE_URL`. Compare
against the [production render](../../deploy/README.md#read-only-preflight).
The control plane and every noded must have empty store endpoints and buckets;
store credential references must be absent. No dev path or callback may point
at production's identities. Image placeholders in the source chart prevent
using this render as an installation artifact.

On the hub context, inspect the live selection without changing it:

```bash
kubectl get application embervm-dev -n argocd -o json | jq '{
  chart: .spec.sources[0].targetRevision,
  values: .spec.sources[0].helm.valueFiles,
  destination: .spec.destination,
  sync: .status.sync.status,
  health: .status.health.status
}'
kubectl get deployments -n embervm-dev -o json | jq '.items[] | {
  name: .metadata.name,
  replicas: .spec.replicas,
  selector: .spec.template.spec.nodeSelector,
  tolerations: .spec.template.spec.tolerations
}'
kubectl get pods,pvc,workloads -n embervm-dev
kubectl get deployment embervm-dev-embervm -n embervm-dev -o json | \
  jq '[.spec.template.spec.containers[] | select(.name == "control-plane") |
    .env[] | select(.name == "EMBERVM_BRICK_CLASSES" or
      .name == "EMBERVM_NODE_CONFIRMED_DESTROY" or
      .name == "EMBERVM_SPEC_TRACE" or
      .name == "EMBERVM_STORE_ENDPOINT" or .name == "EMBERVM_STORE_BUCKET")]'
```

The class configuration must preserve the `2gi` floor and allow only the
bounded `4gi` canary. A live `4gi` Deployment is therefore not itself an
isolation failure. Using existing authorized management access, compare
`GET /v1/nodes` on each control plane: brick identities must be disjoint and
their dial-home destination must match their namespace. Spot replacement
can change inventories during observation, so an unchanged production node
list is not a reliable isolation test.

These checks inspect configuration and inventory. They do not run fault
injection or establish recovery acceptance. Honeycomb holds admitted hub
telemetry; Kargo is at `private.jomcgi.dev/app/kargo`. There is no ArgoCD UI
route. The [quarantine runbook](../../../../docs/runbooks/embervm-stateful-generation-quarantine.md)
covers stateful generation refusals.

## Inactive presets replace this release's configuration

Both presets target the same `embervm-dev` release and namespace. Neither is
referenced by the normal hub Application. Activating one replaces the normal
configuration through a separately reviewed change, with its own operational
acceptance; do not install a competing release.

| Preset | Value order after chart defaults | Store and isolation |
| --- | --- | --- |
| [Recovery](values-recovery-gke.yaml) | Recovery file only | Local-only store, recovery scratch/rootfs and broker identities, isolated `monolith-dev` Postgres op-log; planned credentials require verification |
| [Store validation](values-store-validation-gke.yaml) | Dev `values.yaml`, dev `values-gke.yaml`, then validation file | GCS `h0melab-ember-bases-dev`, required dev Secret references, application deletion gates disarmed |

The [store-validation operator guide](../../deploy/store-validation/README.md)
records #6193's outstanding bucket, IAM, lifecycle, budget and live checks.
Rendering a preset creates none of those external resources. Never borrow
production's GCS bucket, op-log, rootfs paths, broker grants or credentials
to make an inactive preset start.
