# loom hub deployment

Loom source lives in `weave-hand/loom`. This directory holds the cluster
wiring for loom and its Postgres control plane: the `loom` namespace, `loom-pg`
CNPG cluster, daily backup, 1Password references and metrics Service.
Loom runs its own sqlx migrations and needs no Postgres extensions. CNPG creates
the `loom-pg-app` Secret for application access.

The Application at `projects/gke-apps/loom` is registered in
`projects/gke-apps/kustomization.yaml`. Its activation PR must remain draft
until the package, 1Password item and R2 bucket checks below are recorded on
#6605. Registration alone does not verify a rollout. Its three sources combine
the upstream chart, this repo's values and this directory's kustomize
manifests. The manifest guard
checks that resource set, and Linux CI renders the pinned chart and validates
every resource against the pinned Kubernetes and operator schemas. The ArgoCD
OCI repository credential lives in `projects/gke-apps/loom`, not here, so the
hub root applies it before the Application pulls the private chart.

## Chart and images

The Application pins chart `loom` to `0.2.0` from
`ghcr.io/weave-hand/charts` (weave-hand/loom#691). Version `0.1.0` has the June
MVP templates and lacks the required engine, worker, UI, migrations, S3 and
image-pull-secret values. `0.0.0-edge` and `bleeding-edge` are forbidden by
#6603. The hermetic render uses upstream source commit
`15b16649e851331b9f912e87666e724747f4c32d`, with its archive checksum pinned
in `MODULE.bazel`. This source declares chart version `0.2.0`; package
publication remains an operator check.

All four images use `sha-e6ca13c` with `digest: ""`. Bump all four tags
together to one upstream main `sha-<short>` and keep the chart pin's templates
in step with that commit. `worker.replicas` stays at zero: at `e6ca13c`,
`.github/workflows/release.yml` delegates main-push publishing to
`buildbuddy.yaml` and has no main-push trigger. #6605 verifies the worker image
before enabling the queue drainer. The rendered worker pod still has explicit
worker and engine resources.

Loom uses the CNPG-generated `loom-pg-app` Secret. On-boot migrations avoid
the ArgoCD hook/health deadlock. The R2 warehouse is `s3://loom`, region `auto`,
on the existing account endpoint. The operator syncs `r2-s3-credentials` to
`loom-s3-credentials`; chart values map its `access-key-id` and
`secret-access-key` fields. The chart creates the `loom` ServiceAccount with
`ghcr-imagepull-secret`. No hand-written ServiceAccount or S3 Secret is needed.
The staged private routes serve the UI and API at
`private.jomcgi.dev/app/loom/`, rewriting the prefix and redirecting the bare
path to its trailing slash. Both carry `ingress-tier: trusted`.
`loom-private-cf-access` targets the backend-serving `loom-private` route in
the `loom` namespace, validates `Cf-Access-Jwt-Assertion` with the shared
Cloudflare issuer and JWKS settings, and projects `email` to `X-Auth-Email`.
The slash route only redirects and has no backend. Runtime enforcement must be
verified after the Application rolls out.

## Operator checks (#6605)

- Confirm chart `0.2.0` and all four `sha-e6ca13c` images exist in ghcr. The
  agent token cannot read packages; CI validates source rendering only.
- Before merging activation, confirm `argocd-repo-weave-hand-charts`,
  `r2-s3-credentials` and the existing image-pull item exist in 1Password.
  After ArgoCD creates their references, verify sync into
  `repo-weave-hand-charts` in `argocd`, `loom-s3-credentials` in `loom` and
  `ghcr-imagepull-secret` in `loom`. Check metadata without printing credentials.
- Confirm the R2 bucket `loom` exists before enabling the Application.
- Merge the Application registration through Git after those checks. Keep
  `worker.replicas: 0` until its image check is recorded, then enable it in a
  separate PR. Verify rollout, migrations, queue draining and the private UI/API.
  #6605 stays open for bootstrap, backup, restore and soak acceptance.

## Backup credential

Loom reuses `vaults/k8s-homelab/items/cnpg-gcs-backups-context-forge`, whose
`service-account-key.json` field belongs to
`cnpg-backup-context-forge@h0melab.iam.gserviceaccount.com`. The 1Password
Operator syncs it to `loom-pg-backup-gcs`. No new item, service account or IAM
binding is needed. A dedicated `cnpg-backup-loom` service account is the
follow-up if per-cluster isolation is wanted.

Base backups and gzip-compressed WAL use
`gs://h0melab-cnpg-backups/loom-pg/`, with 14-day retention and a daily base
backup at 02:00 UTC. Check that the first base backup lands under this prefix
when #6605 enables the cluster.

## Staged monitoring (#6604)

`projects/platform/otel-collector/values.yaml` defines `loom.enabled: false`.
Neither production overlay enables it. The flag appends an in-cluster GET probe
to `http://loom-query-api.loom.svc:8080/docs` and creates the backup checker in
the collector release namespace. The pinned Loom chart renders that Service on
8080 with no NetworkPolicies. Cluster reachability remains a live check.
Switch `loom.probeEndpoint` to `/healthz` once weave-hand/loom#694 ships;
`/docs` currently checks HTTP reachability only.

The checker is a native `batch/v1` CronJob: Argo Workflows is single-namespace
and watches only `monolith-workflows`, so it cannot run a CronWorkflow here.
Every 30 minutes the checker's own ServiceAccount gets only the named CNPG
Cluster `loom-pg` in `loom`. It reads `status.lastSuccessfulBackup`, computes
age in seconds and POSTs `cnpg.backup.last_success_age_seconds` to the
collector's OTLP/HTTP Service on 4318. A cluster with no successful backup ages
from `metadata.creationTimestamp` and carries
`cnpg.backup.has_successful_backup=false`. API and export failures exit non-zero.
The flag requires `httpcheck.enabled` and a non-empty `allowedServices` so both
probe and OTLP paths exist; the GKE overlay already supplies these.

The 9187 metrics Services are not scraped by this collector, which has no
Prometheus receiver. Probe and backup-age telemetry can be inspected in
Honeycomb after enablement. There are no Loom probe, backup-age or
checker-absence trigger specs. The
[Honeycomb one-trigger policy](../../platform/honeycomb/README.md) reserves the
only trigger for the public health composite.

The [shared backup advisory](../../../docs/observability.md#staged-shared-backup-advisory)
is staged off under #7002. It can include Loom when `loom.enabled` is on;
its producer status and monolith reader have separate default-off gates.
Missing or stale checker observations report unknown without a new
checker-absence notification. This does not provide a Loom probe alert.

Enable telemetry in this order, through separate PRs:

1. Verify the Loom Application is Synced and Healthy during #6605. Monitoring
   is a follow-up acceptance step, so #6605 need not be closed first.
2. Flip `loom.enabled` in the GKE overlay through a PR and verify the collector
   rollout, the Loom probe and the backup CronJob's API access and export.
3. Confirm successful probe datapoints and the age gauge, including the
   cluster, namespace, environment and fallback attributes, in Honeycomb.

Monitoring acceptance remains in #6604, application activation in #6605 and
shared backup advisory acceptance in #7002. Application registration leaves
monitoring disabled and creates no Honeycomb alerts.

## Restore into a new cluster

Make every change through a PR. ArgoCD applies the configuration after the
activation PR merges. Never use `kubectl apply`.

Create a new Cluster, for example `loom-pg-r1`. Reusing the old name requires
deleting the old Cluster through Git first; keep its backups. Preserve the
namespace, storage and resources from `cnpg-cluster.yaml`, replace
`bootstrap.initdb` with recovery, and add this external source:

```yaml
bootstrap:
  recovery:
    source: loom-pg
    database: loom
    owner: loom
externalClusters:
  - name: loom-pg
    barmanObjectStore:
      destinationPath: gs://h0melab-cnpg-backups/loom-pg/
      serverName: loom-pg
      googleCredentials:
        applicationCredentials:
          name: loom-pg-backup-gcs
          key: service-account-key.json
      wal:
        compression: gzip
```

Keep the restored cluster's backup configuration, but set its
`backup.barmanObjectStore.serverName` to a new archive name such as `loom-pg-r1`.
The recovery source stays `loom-pg`. Never write into the archive being restored:
CNPG refuses a non-empty destination archive. Use a fresh archive name for each
restore attempt. Update the ScheduledBackup target and metrics selector to the
restored Cluster name, and use its CNPG-generated application Secret.

After rollout, verify recovery completed, loom's migrations and queue work, and
a new base backup and WAL archive land under the restored cluster's archive
name. `projects/mcp/context-forge-gateway/deploy/values-gke.yaml` records the
existing recovery pattern.
