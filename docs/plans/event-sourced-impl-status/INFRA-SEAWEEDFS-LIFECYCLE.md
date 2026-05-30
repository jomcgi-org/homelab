# INFRA-SEAWEEDFS-LIFECYCLE — warehouse stale-artifact lifecycle policy

Status: **author-only** (CI-validated, NOT wired into ArgoCD this run). Wiring
into `projects/platform/kustomization.yaml` /
`projects/home-cluster/kustomization.yaml` is **deferred to the orchestrator**
(central wiring after merge), per the unit's "purely additive" constraint.

## What shipped

A new, additive ArgoCD Application `warehouse-lifecycle` that idempotently
applies a stale-serving-artifact **expiry (lifecycle) policy** to the existing
`warehouse` S3 bucket on the existing SeaweedFS deployment (ns `seaweedfs`).
Nothing under `projects/platform/seaweedfs/` or
`projects/platform/warehouse-bucket/` was touched.

Files created (all new, in `projects/platform/warehouse-lifecycle/`):

- `application.yaml` — `argoproj.io/v1alpha1` Application, name
  `warehouse-lifecycle`, ns `argocd`, label
  `app.kubernetes.io/part-of: shared-infrastructure`, annotation
  `argocd.argoproj.io/sync-wave: "1"`. Plain kustomize/directory source (NO Helm
  block): `repoURL https://github.com/jomcgi/homelab.git`,
  `path projects/platform/warehouse-lifecycle`, `targetRevision HEAD`.
  Destination ns `seaweedfs`. `syncPolicy.automated` prune + selfHeal,
  `syncOptions: [CreateNamespace=true]`, retry limit 5.
- `kustomization.yaml` — Kustomization, `resources: [job.yaml]`.
- `job.yaml` — the idempotent lifecycle-apply Job (details below).

## Lifecycle approach — PREFIX-based TTL (documented deviation from tag-filter)

The unit spec preferred an S3 `PutBucketLifecycleConfiguration` rule with a
`Filter` on tag `state=stale` + `Expiration: Days=1`. **That is not safe on the
deployed SeaweedFS, so this unit took the spec-anticipated prefix-based
fallback.** Research findings:

- **Deployed version is SeaweedFS 3.73** (`appVersion` in
  `projects/platform/seaweedfs/Chart.yaml`; the chart/values do not override the
  image tag — same fact the sibling INFRA-SEAWEEDFS unit relied on).
- **Reliable S3 lifecycle expiration is NOT available on 3.73.** Upstream
  maintainer (chrislusf) states dependable lifecycle expiry only landed "around
  4.26~" (seaweedfs#2745). On recent versions the S3-lifecycle age check is
  buggy: seaweedfs#6619 reports a prefix-filtered 1-day rule that deleted **all**
  matching objects regardless of age. Tag-filtered (`Filter.Tag`) expiry is even
  less battle-tested than prefix. Putting an S3 lifecycle config on 3.73 would
  therefore either silently no-op or risk mass-deletion of the durable
  warehouse — neither acceptable.
- **The version-stable native mechanism in 3.73 is `weed shell fs.configure`**,
  which has a `-ttl` flag (verified present in the 3.73 source:
  `weed/shell/command_fs_configure.go`, `String("ttl", ...)`, regex
  `^[1-255][mhdwMy]$`) applied per `-locationPrefix` and persisted with `-apply`.
  This sets a **filer-enforced, age-correct TTL** on a path. S3 buckets map to
  filer path `/buckets/<bucket>/`, so the serving prefix is
  `/buckets/warehouse/serving/`.

So the policy is: `fs.configure -locationPrefix=/buckets/warehouse/serving/
-ttl=1d -apply`. This is **prefix-based, not tag-based** — the exact fallback
the unit spec allowed ("if tag-based filtering isn't supported … fall back to a
prefix-based rule on `serving/` with documented caveat").

### Freshness caveat

A prefix TTL on `serving/` ages out the _current_ artifact after 1 day too, not
just `state=stale` ones. This is fine because `BuildServingArtifactWorkflow`
rewrites the current artifact every ~15 min (ADR platform/004), far inside the
1-day window — the live artifact is continuously refreshed and only
orphaned/stale objects actually age out. The build workflow's **keep-last-N=24**
cleanup remains the belt-and-suspenders backstop (ADR platform/004 §"Serving
artifact lifecycle"). When SeaweedFS is upgraded to >= ~4.26 this can be
revisited and swapped for a true tag-filtered (`state=stale`) S3 lifecycle rule.

## Idempotency & security

Mirrors the sibling INFRA-SEAWEEDFS `warehouse-bucket` Job:

- Same image `chrislusf/seaweedfs:3.73`, talks to
  `seaweedfs-master.seaweedfs.svc.cluster.local:9333` via `weed shell`. No S3
  credentials (S3 gateway `enableAuth: false`; `weed shell` talks to the master
  directly over the cluster network).
- **Idempotent**: `weed shell` is a command interpreter (cannot branch), so the
  Job first reads the persisted config with a flag-less `fs.configure`
  (simulation, no mutation), greps for the prefix + desired ttl, and only re-runs
  with `-apply` if absent. Safe to re-run; ArgoCD recreates it each sync via
  `argocd.argoproj.io/hook: PreSync` + `hook-delete-policy: BeforeHookCreation`
  (avoids the "Job spec is immutable" selfHeal problem).
  `ttlSecondsAfterFinished: 600` reaps the pod.
- **Security**: non-root uid/gid 65532, `runAsNonRoot`,
  `readOnlyRootFilesystem: true` (emptyDir at `/tmp`, `HOME=/tmp` for `weed`
  scratch), `allowPrivilegeEscalation: false`, all capabilities dropped,
  `seccompProfile: RuntimeDefault`, `automountServiceAccountToken: false` (no
  K8s API calls). `restartPolicy: OnFailure`, `backoffLimit: 5`, minimal
  resources (25m/32Mi req, 100m/64Mi lim). Linkerd-meshed namespace, NO
  NetworkPolicies (per cluster rule).

## ADR alignment

- Implements ADR platform/004 §"Serving artifact lifecycle": "SeaweedFS
  lifecycle deletes `state=stale` objects after 1 day" — realized here as a
  1-day prefix TTL on `serving/` (deviation documented above), with keep-last-N
  as the documented backstop.
- §"One operational rule" (snapshot GC / Iceberg expiry outside the backup
  window) concerns **Iceberg snapshot GC**, a separate workflow — out of scope
  for this serving-artifact lifecycle unit; noted so it isn't conflated.

## Deferred / follow-ups

- **Central wiring** of this Application into
  `projects/platform/kustomization.yaml` /
  `projects/home-cluster/kustomization.yaml` — handled by the orchestrator after
  merge (per unit constraints; this unit must not edit those files).
- **Tag-filtered S3 lifecycle** (`state=stale`, Expiration Days=1) — revisit
  after a SeaweedFS upgrade to >= ~4.26, where it becomes reliable.

## Self-validation

`kubectl kustomize projects/platform/warehouse-lifecycle/` renders cleanly
(single Job manifest). All three files pass YAML lint. No Helm here. Nothing was
applied to the cluster and `fs.configure` was NOT run against the live filer —
author-only, per unit constraints. (Live kubectl re-confirmation of service
names/ports was not performed; the verified discovery facts from the sibling
unit were reused: `seaweedfs-master.seaweedfs:9333`, S3 auth disabled, image
`chrislusf/seaweedfs:3.73`.)
