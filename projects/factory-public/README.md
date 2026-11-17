# Factory public mirror

This chart owns Garage's factory-public bucket and its R2 mirror (Refs HOME-137).
Its single bucket entry is in chart/values.yaml. ArgoCD deploys it in the
factory-public namespace through projects/platform-factory/kustomization.yaml.
The CronJob is suspended. It has no publisher or public route.

The configured destination is factory-public and the requested public domain is
https://factory-data.jomcgi.dev. Neither the bucket nor its credential is verified.
The configured 1Password item is
vaults/k8s-homelab/items/factory-r2-public-mirror. The item needs access-key-id,
secret-access-key and s3-api-endpoint. Its R2 token needs object read/write
permission on this bucket. The endpoint must match the existing homelab account:
https://7c56b458cd657d96b095c63d181c051f.r2.cloudflarestorage.com.
The linked HOME-65 comment was unavailable to this worker; the R2 bucket name
uses the Garage bucket name from the delegated issue. The deployment keeps
public.suspend: true until the landing owner records the prerequisite checks
below and enables it through a values change.

## Provisioning result

The implementation uses HOME-66's garage-init fallback. The existing
initializer reads authorization ConfigMaps only in the protected `garage`
namespace. The single bucket entry renders them with the bound consumer namespace,
Secret name, bucket and access. An admission policy permits changes only from the
authenticated ArgoCD application controller. Consumer ConfigMaps and mutable
Secret labels cannot grant bucket access. Secret access stays restricted to the
named Garage keys; discovery never grants Secret list permission.

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

This worker has no configured cluster connection or credentials. No live operator
trial ran, and this result establishes no operator defect. The fallback keeps
Garage's existing layout and key ownership, with no unverified operator rollout.
The library README describes the authorization boundary and Secret permissions.

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
Kubernetes API server. It impersonates API callers with create/update rights
and tests Pods, Jobs and CronJobs, including forged owners and reserved
ServiceAccounts. It also checks garage-init's rendered RBAC: named Garage
Secrets are readable and writable, while Secret lists, R2 Secret access and
consumer ConfigMap discovery are denied. It gives a simulated attacker ConfigMap
write rights and verifies that bucket/access changes, binding changes, label
removal, deletion and forged declarations are denied in `garage`. ArgoCD requests
remain allowed. Python tests verify that reconciliation reads only these protected
declarations and rejects invalid bindings before granting access. These tests use
no cluster credentials.

## HOME-137 acceptance evidence

| Requirement from the delegated issue | Checkable evidence | State |
| --- | --- | --- |
| Criterion 1: golden defaults and refusals for tags, missing itemPath and undeclared egress | [test_public_bucket_defaults and test_refusals](../shared/helm/factory-library/library_test.py), [default golden](../shared/helm/factory-library/goldens/buckets.yaml), [refusals](../shared/helm/factory-library/goldens/refusals.yaml) | Covered by local Helm/pytest checks. |
| Criterion 2: adding a public bucket is one values entry | [chart/values.yaml](chart/values.yaml); [chart/public_test.py](chart/public_test.py) asserts a single buckets entry and the rendered resources | Implemented, with this destination suspended. |
| Criterion 3: public object within two minutes, cache headers and deletion | Live acceptance below | Pending verified prerequisites and rollout. |

The library implements the bucket, 1Password item, mirror, egress and admission
resources. Local renders and tests do not establish completion of the live
criterion. The operator trial could not reach the live cluster; the fallback
is documented above. HOME-137 remains operationally incomplete until the
landing owner records the live evidence.

Review round 6 on 2026-11-17 leaves `HOME-137:goal` and
`HOME-137:goal:criterion-3` unresolved. This binding has no kubeconfig, Kubernetes
service endpoint or 1Password connection. DNS lookups for factory-plan.jomcgi.dev,
factory-data.jomcgi.dev and garage.garage.svc fail with `Name does not resolve`.
No live object, cache-header, timing or deletion evidence can be collected here.
The live criterion remains part of HOME-137; this PR does not move or waive it.

## Before enabling the first mirror

Keep public.suspend: true while any prerequisite is unknown. Record evidence
on HOME-137 without credential values:

1. The 1Password operator resolves vaults/k8s-homelab/items/factory-r2-public-mirror
   and creates the expected Secret with all three required fields.
2. The item's endpoint equals the declared account endpoint above. Confirm the
   credential is scoped to object read/write on the dedicated factory-public
   R2 bucket, using the Cloudflare token configuration or provider metadata.
3. Confirm that the bucket is the intended dedicated destination. Record an
   authenticated inventory showing that it contains no unrelated objects or
   data that must be retained. Do not run sync to test this prerequisite.
4. Confirm the public domain points to that bucket and that Garage has the
   intended source data. Record the controller ServiceAccount identities.
5. Change public.suspend to false in Git after those checks. ArgoCD applies it.

No live credentials or inventory are available in this worker binding. The
suspended deployment cannot provide evidence for these external prerequisites.

## Live acceptance

Live acceptance remains pending prerequisite verification and activation. The landing owner must record:

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
