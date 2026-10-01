# loom Postgres deployment

Loom source lives in `weave-hand/loom`. This directory holds only the cluster
wiring for loom's Postgres control plane: the `loom` namespace, `loom-pg` CNPG
cluster, daily backup, 1Password credential reference and metrics Service.
Loom runs its own sqlx migrations and needs no Postgres extensions. CNPG creates
the `loom-pg-app` Secret for application access.

This configuration is default-off. No Application or `projects/gke-apps`
entry references it. #6603 adds the Application; #6605 enables it. The manifest
guard checks the kustomize resource set, and Linux CI validates each resource
against the pinned Kubernetes and operator schemas.

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
