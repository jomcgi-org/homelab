# Loom on GKE

Deploy the same released Loom chart to dev on Spot and production on core.
This is the storage and service foundation for homelab issue #5991; extraction,
agent identity integration, and KEDA are subsequent work.

| Environment | Application | Namespace | Node pool | Warehouse PVC | Database / owner |
| --- | --- | --- | --- | --- | --- |
| Dev | loom-dev | loom-dev | ember-bricks (Spot) | 5Gi | loom_dev |
| Prod | loom-prod | loom | core-e2 (on-demand) | 10Gi | loom |

Both databases live in the existing `monolith-pg` CNPG cluster. Separate
DatabaseRole resources own only their respective database; no additional
Postgres cluster is created. Four service pools are capped at five connections
each (20 per environment), leaving connection capacity for existing clients.
CNPG backs up both databases under its existing
cluster backup policy. The warehouse requires its own backup and recovery plan
before this deployment becomes authoritative for organizational knowledge.

## Release and promotion

Prerequisite: merge https://github.com/weave-hand/loom/pull/691 and wait for
BuildBuddy to publish chart `0.2.0`. Registry `0.1.0` is the older DuckLake chart;
`0.0.0-edge` is mutable and is deliberately not deployed. Verify the published
0.2.0 chart before merging/enabling this deployment. The upstream release build
pins the service images into the chart.

Renovate watches `ghcr.io/weave-hand/charts/loom` using its Docker/OCI datasource
and opens reviewed PRs advancing the exact `semverConstraint` in
`projects/platform/kargo/values-gke.yaml`. It accepts stable SemVer releases
only. This single approval input prevents Kargo from bypassing a pending
Renovate review. Renovate does not edit the Kargo-owned Application pins.

The GKE-only `kargo-loom` pipeline discovers that approved version, syncs dev,
waits for Synced/Healthy, then makes it available to prod after a ten-minute
soak. Prod has no automated ArgoCD sync: Kargo initiates its sync, including the
first deployment, only after dev promotion succeeds. Configuration-only prod
changes therefore need an explicit ArgoCD sync. The gate checks rollout health,
not extraction quality or functional correctness. Spot shortages can hold the
dev gate; they never justify silently bypassing it.

The Application versions in git are bootstrap floors. Kargo owns the live
versions. Existing pipelines retain their behavior. Replacing all chart-version
write-back or Git promotion machinery is outside this change.

## First rollout prerequisites

1. Publish and inspect the upstream 0.2.0 chart as described above.
2. Create two distinct 1Password items under `k8s-homelab`: `loom-dev-db` and
   `loom-prod-db`. Each needs `host=monolith-pg-rw.monolith.svc`, `port=5432`,
   `username`, `password`, and `dbname`. Dev's username/dbname are `loom_dev`;
   prod's are `loom`. Use different generated passwords. Never commit them.
   The bootstrap chart projects each item as a CNPG basic-auth secret in
   `monolith` and an application connection secret in its target namespace.
   The installed OnePasswordItem CRD supports the top-level `type` field.
3. Confirm the existing `ghcr-read-permissions` 1Password item can pull the
   `weave-hand` images. The chart itself must be readable by ArgoCD and Kargo;
   it was anonymously readable during preparation. No new registry token is
   embedded in git.
4. Apply the reviewed `projects/gke-cluster/root-application.yaml` once using
   the operator's GKE context. This bootstrap Application does not manage itself.
   Its new ignoreDifferences entries plus RespectIgnoreDifferences stop the root
   reverting Kargo's Loom versions. Apply before the new pipeline is enabled.
5. Sync the root and `loom-bootstrap`, verify the two DatabaseRole and Database
   resources are ready, then let Kargo promote dev followed by prod. Bootstrap
   resources live in sync wave 1 and workload Applications in wave 2. CNPG and
   1Password reconciliation are asynchronous; missing items must be resolved
   before expecting the pods to become ready.
6. Bootstrap each environment's first admin with upstream's `loom create-admin`
   procedure using that environment's database credential, then create the
   required grants/service identities. The Helm chart does not create an admin.

No public ingress is configured. Inspect dev with
`kubectl -n loom-dev port-forward service/loom-query-api 8080:8080`, or prod with
`kubectl -n loom port-forward service/loom-query-api 8080:8080`.
Production permits internal HTTP clients from `monolith` and `monolith-agents`;
dev permits only same-namespace pods. Both allow database egress only to
`monolith-pg` pods, plus DNS. This network boundary does not replace Loom ACLs.

## Scheduling and capacity

Ingest, query-api plus engine, and worker plus engine all mount the environment's
warehouse. Upstream's required pod affinity keeps them on the ingest node, which
allows the shared ReadWriteOnce disk to work. Dev selects `ember-bricks` and
tolerates its Ember taint; prod selects `core-e2` and has no Spot toleration.
Keep ingest at one replica until that affinity dependency is redesigned.

Each environment requests 950m CPU and 2944Mi memory across five containers.
Memory limits total 6400Mi. These are starting budgets, not measured performance
guarantees; index builds and transforms need monitoring and bounded concurrency.
The built-in worker stays at one replica for this rollout so flush, compaction,
indexing and garbage collection always drain. Extraction is not installed here.

## Storage growth and recovery

Both PVCs use the existing GKE `standard-rwo` Persistent Disk CSI storage class,
which has `WaitForFirstConsumer` and `allowVolumeExpansion=true`. A Spot node
loss does not delete the dev warehouse disk; reattachment can delay recovery.

Increase `objectStore.size` in the appropriate environment values file and sync
the Application (explicitly for prod). The PVC name and storage class must stay
unchanged. Verify capacity and filesystem expansion before resuming large loads.
Shrinking is unsupported. Upstream sets `helm.sh/resource-policy: keep`; database
and role resources also use retain policies. Do not delete retained volumes to
resolve a rollout or migration failure.

A rollback must consider database migrations as well as the chart version.
Do not blindly promote an older chart against a newer schema. Before upgrading
with valuable data, capture recoverable database and warehouse state and test
restoration together. CNPG backup alone does not contain the Parquet files.

## Validation

Render the bootstrap chart, both Loom environment overlays against the candidate
upstream chart, both shared/GKE Kargo configurations, and the GKE Kustomize root.
Check that prod is gated by dev, Renovate has exactly one version authority,
secrets/databases differ, and every warehouse consumer selects the right pool.
Linux CI is the merge gate; do not run Bazel or full suites on macOS.
