# EmberVM dev on the GKE hub

EmberVM is retired. HOME-476 removed `embervm-dev` and its Kargo stage from
`homelab-hub`. The [hub removal](#hub-removal-home-476) procedure deletes
what the prune leaves. The design is in
[../../ARCHITECTURE.md](../../ARCHITECTURE.md).

## Hub removal (HOME-476)

The `embervm-dev` Application has no resources finalizer. The `hub`
Application's prune therefore deletes only the Application object. The
`embervm-dev` namespace and the release's ClusterRole and ClusterRoleBinding
stay. The shared `workloads.embervm.dev` CRD can also stay.

An operator with hub write access does these steps in order after the
HOME-476 merge. Set `HUB_CONTEXT` to the hub's kubectl context.

1. Confirm that Argo CD pruned both EmberVM Applications.

   ```sh
   kubectl --context "$HUB_CONTEXT" -n argocd get application embervm embervm-dev
   ```

   Both names must return `NotFound`. `embervm` has the resources finalizer,
   so its `NotFound` also shows that its cascade delete finished. If an
   Application remains, stop and use the
   [OutOfSync runbook](../../../../docs/runbooks/argocd-outofsync.md).

2. Delete the `embervm-dev` namespace.

   ```sh
   kubectl --context "$HUB_CONTEXT" -n embervm-dev get deployments,workloads,pvc
   kubectl --context "$HUB_CONTEXT" delete namespace embervm-dev --timeout=10m
   kubectl --context "$HUB_CONTEXT" get pv -o jsonpath='{range .items[?(@.spec.claimRef.namespace=="embervm-dev")]}{.metadata.name}{"\n"}{end}'
   ```

   The first command lists the dev Deployments, Workloads and the 1Gi
   `embervm-dev-embervm-oplog` PVC. The namespace delete removes all of them,
   with the PVC's `standard-rwo` disk. Joe allowed the op-log disposal
   ([decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-476#comment-5552)).
   The `get pv` command must print nothing. If the namespace stays
   `Terminating`, read its `.status.conditions` to find the blocking object.

3. Delete the release's cluster-scoped RBAC.

   ```sh
   kubectl --context "$HUB_CONTEXT" get clusterrole,clusterrolebinding -l app.kubernetes.io/instance=embervm-dev
   kubectl --context "$HUB_CONTEXT" delete clusterrole,clusterrolebinding -l app.kubernetes.io/instance=embervm-dev
   ```

   The `get` must list only `clusterrole/embervm-dev-embervm` and
   `clusterrolebinding/embervm-dev-embervm`. The dev render has no other
   cluster-scoped object except the CRD. After the delete, the `get` prints
   `No resources found`.

4. Delete a leftover `workloads.embervm.dev` CRD.

   ```sh
   kubectl --context "$HUB_CONTEXT" get crd workloads.embervm.dev
   kubectl --context "$HUB_CONTEXT" get workloads.embervm.dev --all-namespaces
   kubectl --context "$HUB_CONTEXT" delete crd workloads.embervm.dev
   ```

   Both releases rendered this CRD, and Argo CD tracked it to the release that
   synced last. If the first command returns `NotFound`, the `embervm` cascade
   deleted the CRD: go to step 5. Otherwise the second command must print
   `No resources found`, because a CRD delete also deletes every Workload.
   Then run the delete. No hub consumer reads the CRD after HOME-476.

5. Check the `ember-bricks` Spot pool.

   ```sh
   kubectl --context "$HUB_CONTEXT" get nodes -l cloud.google.com/gke-nodepool=ember-bricks
   for node in $(kubectl --context "$HUB_CONTEXT" get nodes -l cloud.google.com/gke-nodepool=ember-bricks -o name); do
     kubectl --context "$HUB_CONTEXT" get pods --all-namespaces --field-selector "spec.nodeName=${node#node/}"
   done
   ```

   `gcloud` manages this pool outside GitOps, and its autoscaler removes
   unneeded nodes. No pod in `embervm` or `embervm-dev` may remain. Such a pod
   means that an earlier step did not finish. The pool reaches zero nodes only
   when no other pod needs it. The hub `inference` embeddings pod selects
   `embervm.io/serving: "true"`, and only brick nodes carry that label
   ([values-gke.yaml](../../../inference/deploy/values-gke.yaml)). That pod
   keeps one node until it moves. DaemonSet pods and Argo batch pods at
   priority `homelab-disposable` can also appear. They do not keep a node.

## The Application defined the value order

The removed hub Application used release and namespace `embervm-dev`, with
chart defaults followed by
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
- Fixed `2gi` and `8gi` bricks on the shared `ember-bricks` Spot pool. There is no
  hostname pin. The selector is `homelab.io/firecracker: "true"`, with the
  `embervm.jomcgi.dev/node=true:NoSchedule` toleration. A bounded `4gi` canary
  can open on demand under the current full-mode autoscaler. Zero rendered
  replicas alone do not describe the controller's eventual target.
- The `sandbox-python`, `pi-runtime`, and `sandbox-elixir` workloads, the
  bounded 4096 MiB `claude-runtime` session for S9, and the conformance runner.

Node-confirmed destroy ordering is unconditional in every release.
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
      .name == "EMBERVM_SPEC_TRACE" or
      .name == "EMBERVM_STORE_ENDPOINT" or .name == "EMBERVM_STORE_BUCKET")]'
```

The class configuration must preserve the `2gi` and `8gi` floors and allow only
the bounded `4gi` canary. A live `4gi` Deployment is therefore not itself an
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
