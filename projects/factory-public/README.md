# Factory public mirror

This chart owns Garage's factory-public bucket and its R2 mirror (Refs HOME-137).
Its single bucket entry is in chart/values.yaml. ArgoCD deploys it in the
factory-public namespace through projects/platform-factory/kustomization.yaml.
It has no publisher or public route.

The R2 bucket is factory-public and its public domain is
https://factory-data.jomcgi.dev. The 1Password item is
vaults/k8s-homelab/items/factory-r2-public-mirror. The item needs access-key-id,
secret-access-key and s3-api-endpoint. Its R2 token needs object read/write
permission on this bucket. The endpoint must match the existing homelab account:
https://7c56b458cd657d96b095c63d181c051f.r2.cloudflarestorage.com.
The linked HOME-65 comment was unavailable to this worker; the R2 bucket name
uses the Garage bucket name from the delegated issue. Live acceptance must
confirm that the item and Cloudflare bucket use these values.

## Provisioning result

The implementation uses HOME-66's garage-init fallback. The existing
initializer now discovers labeled Secrets and reconciles each access grant.

The operator trial was limited to source and chart rendering on 2026-11-17.
Upstream commit 28f953ad52aa9dcfe5b7922a31d1e7bb6301f910 supports a
connectTo-only management handle without creating Garage workloads.
Its chart rendered locally with helm template. The intended first trial was
factory-public, using this handle:

```yaml
apiVersion: garage.rajsingh.info/v1beta2
kind: GarageCluster
metadata:
  name: factory-public-trial
  namespace: garage
spec:
  connectTo:
    adminApiEndpoint: http://garage-admin.garage.svc:3903
    adminTokenSecretRef:
      name: garage-secrets
      key: admin-token
    rpcSecretRef:
      name: garage-secrets
      key: rpc-secret
```

The release pull failed:

```text
helm pull oci://ghcr.io/rajsinghtech/charts/garage-operator --version 0.9.0 --destination /tmp
dial tcp: lookup ghcr.io on 169.254.17.1:53: no such host
```

This worker has no Kubernetes client or cluster credentials. No live operator
trial ran, and this result establishes no operator defect. The fallback keeps
Garage's existing layout and key ownership, with no unverified operator rollout.
The library README describes its namespaced Secret discovery permissions.

## Validation

Run from the repository root:

```bash
ci lint
ci regen
ci ratchet
helm template factory-public projects/factory-public/chart -n factory-public
helm lint projects/factory-public/chart
HELM_BIN="$(command -v helm)" python3 -m pytest \
  projects/shared/helm/factory-library/library_test.py \
  projects/platform-factory/garage/resources/garage_init_test.py \
  projects/factory-public/chart/public_test.py \
  projects/factory-landing/chart/landing_test.py \
  projects/jomcgi-site/chart/site_chart_test.py \
  projects/platform-factory/authentik/resources/backup_test.py
```

PR CI runs //projects/factory-public/admission:admission_test against a private
Kubernetes API server. It tests accepted and denied Pod creates and updates,
including ephemeral containers, without cluster credentials.

## Live acceptance

Live acceptance remains pending deployment. The landing owner must record:

1. ArgoCD Synced/Healthy, initialized Garage Secrets, and a populated R2 Secret.
2. A successful mirror CronJob using the pinned image and its own ServiceAccount.
3. A harmless object written to Garage factory-public, then served at
   https://factory-data.jomcgi.dev/<key> within two minutes.
4. Cache-Control: public, max-age=31536000, immutable on that object.
5. A manifest referencing the object, published after it, with
   Cache-Control: public, max-age=60.
6. Removal of the manifest and then the object from Garage, followed by their
   removal from R2 after the next successful run.

Use a dedicated test key. Public CDN responses can remain cached after an R2
deletion; verify deletion through an authenticated R2 listing as well.
Never print credential fields. No live check or full remote CI run was claimed
by the implementation worker.

The later publisher uses its own buckets entry with write access and no public
field. CORS stays with Joe when the site's hostname is known.
