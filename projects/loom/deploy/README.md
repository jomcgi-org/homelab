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
every resource against the pinned Kubernetes and operator schemas.

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
