# loom hub deployment

Loom source lives in `weave-hand/loom`. This directory holds the cluster
wiring for loom and its Postgres control plane: the `loom` namespace, `loom-pg`
CNPG cluster, daily backup, 1Password references and metrics Service.
Loom runs its own sqlx migrations and needs no Postgres extensions. CNPG creates
the `loom-pg-app` Secret for application access.

This configuration is default-off. The Application at `projects/gke-apps/loom`
is absent from `projects/gke-apps/kustomization.yaml`. #6605 enables it after
the operator checks below. Its three sources combine the upstream chart, this
repo's values and this directory's kustomize manifests. The manifest guard
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
The private gateway serves the UI and API at `private.jomcgi.dev/app/loom/`,
rewriting the prefix and redirecting the bare path to its trailing slash.

## Operator checks (#6605)

- Confirm chart `0.2.0` and all four `sha-e6ca13c` images exist in ghcr. The
  agent token cannot read packages; CI validates source rendering only.
- Confirm `argocd-repo-weave-hand-charts` and `r2-s3-credentials` exist and sync
  into `repo-weave-hand-charts` in `argocd` and `loom-s3-credentials` in `loom`.
  Confirm the existing pull item syncs `ghcr-imagepull-secret` in `loom`.
- Confirm the R2 bucket `loom` exists before enabling the Application.
- Enable the Application through Git, enable the worker after its image check,
  then verify rollout, migrations, queue draining and the private UI/API.

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
Switch `loom.probeEndpoint` and the trigger's `http.url` filter to `/healthz`
once weave-hand/loom#694 ships; `/docs` currently checks HTTP reachability only.

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

Before this change, no repository consumer alerted on CNPG backup age for
monolith-pg, authentik, context-forge or loom-pg. Monolith-pg needs the same freshness and
checker-absence checks. The 9187 metrics Services are not scraped by this
collector, which has no Prometheus receiver.

Three disabled specs in `projects/platform/honeycomb/triggers/` use the
`metrics` dataset and `deployment.environment=homelab-hub`:

| Trigger | Query and threshold |
| ------- | ------------------- |
| `loom query-api successful probe absent` | COUNT of `httpcheck.status=1`, `http.status_class=2xx`, matching the probe's `http.url`, below 1 over 600 seconds, evaluated every 300 seconds |
| `loom-pg backup older than 36h` | MAX of `cnpg.backup.last_success_age_seconds` for `cnpg.cluster.name=loom-pg`, `k8s.namespace.name=loom`, above 129600 seconds over 3600 seconds, evaluated every 1800 seconds |
| `loom-pg backup age metric absent` | COUNT of the same age gauge below 1 over 7200 seconds (four job periods), evaluated every 1800 seconds |

Collector 0.159.0's `receiver/httpcheckreceiver/scraper.go` records
`httpcheck.error` on connection errors and zero for every status class.
Counting successful 2xx datapoints covers transport errors, non-2xx and absent
probes. A non-2xx MAX test alone misses a refused connection. The backup age
trigger also needs its absence companion: MAX cannot fire without a datapoint.
All three notify the existing Discord webhook recipient and require one
exceeded evaluation. They remain disabled until the operator checks pass.

Enable in this order, through separate PRs and the Honeycomb sync workflow:

1. Finish #6605 and verify the Loom Application is Synced and Healthy.
2. Flip `loom.enabled` in the GKE overlay through a PR and verify the collector
   rollout, the Loom probe and the backup CronJob's API access and export.
3. Confirm successful probe datapoints and the age gauge, including the
   cluster, namespace, environment and fallback attributes, in Honeycomb.
4. Enable the trigger specs and apply with `sync.py`. Test-fire probe failure,
   stale backup and missing checker data, and confirm Discord notifications
   and recovery. If the endpoint changes to `/healthz`, change its trigger
   filter in the same PR.

This PR delivers default-off repository configuration. Live acceptance remains
in #6604; it does not enable Loom, sync Honeycomb triggers or close the issue.

## Restore into a new cluster

Make every change through a PR. ArgoCD applies the configuration after it is
enabled. Never use `kubectl apply`.

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
