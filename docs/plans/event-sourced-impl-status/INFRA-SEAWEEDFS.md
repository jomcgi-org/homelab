# INFRA-SEAWEEDFS — warehouse S3 bucket

Status: **shipped** (PR open, auto-merge enabled). Wiring into the
`projects/platform/kustomization.yaml` and `projects/home-cluster/kustomization.yaml`
is **deferred to the orchestrator** (central wiring after merge), per the unit's
"purely additive" constraint.

## What shipped

A new, additive ArgoCD Application `warehouse-bucket` that creates the
`warehouse` S3 bucket on the **existing** SeaweedFS deployment (ns `seaweedfs`).
Nothing under `projects/platform/seaweedfs/` was touched.

Files created (all new):

- `projects/platform/warehouse-bucket/application.yaml` — `argoproj.io/v1alpha1`
  Application, name `warehouse-bucket`, ns `argocd`, label
  `app.kubernetes.io/part-of: shared-infrastructure`, annotation
  `argocd.argoproj.io/sync-wave: "1"`. Plain kustomize/directory source (NO Helm
  block): `repoURL https://github.com/jomcgi/homelab.git`,
  `path projects/platform/warehouse-bucket`, `targetRevision HEAD`. Destination
  ns `seaweedfs`. `syncPolicy.automated` prune + selfHeal,
  `syncOptions: [CreateNamespace=true]`, retry limit 5.
- `projects/platform/warehouse-bucket/kustomization.yaml` — Kustomization,
  `resources: [job.yaml]`.
- `projects/platform/warehouse-bucket/job.yaml` — the idempotent bucket-creation
  Job (details below).

## Bucket-creation approach (option B: `weed shell`)

Chose option B over the `aws` CLI (option A) because it is simpler:

- **Reuses the same image the cluster already runs** — `chrislusf/seaweedfs:3.73`
  (the deployed chart's appVersion; the repo values do not override the image
  tag). No separate aws-cli image to pull, and `weed` is already on every node.
- **No credentials needed.** The S3 gateway has `enableAuth: false` and `weed
shell` talks straight to the master (`seaweedfs-master.seaweedfs.svc.cluster.local:9333`)
  over the cluster network — no dummy AWS creds required.
- Command surface: `s3.bucket.list` / `s3.bucket.create -name warehouse -replication 000`
  piped into `weed shell -master <master>:9333`.

(aws CLI option A was rejected only on simplicity grounds — it would have worked
too, against `http://seaweedfs-s3.seaweedfs.svc.cluster.local:8333` with dummy
creds and `s3 mb || true`.)

## Idempotency

`weed shell` is a command interpreter, not a POSIX shell, so it can't branch
internally. The Job therefore:

1. Waits for the master to be reachable (up to 30 × 5s) via `cluster.check`.
2. Dumps `s3.bucket.list` and greps (whole-word, tolerant of the tree-style
   listing's leading whitespace / trailing metadata) for `warehouse`. If present,
   exits 0 — nothing to do.
3. Otherwise runs `s3.bucket.create`, then re-lists to verify.

Safe to re-run. The Job carries `argocd.argoproj.io/hook: PreSync` +
`hook-delete-policy: BeforeHookCreation`, so ArgoCD recreates it cleanly on every
sync (avoids the "Job spec is immutable" problem of a plain selfHeal'd Job) and
keeps the bucket present before downstream lakehouse components sync.
`ttlSecondsAfterFinished: 600` reaps the completed pod.

Security: non-root uid/gid 65532, `runAsNonRoot`, `readOnlyRootFilesystem: true`
(with an `emptyDir` at `/tmp` and `HOME=/tmp` for `weed`'s scratch),
`allowPrivilegeEscalation: false`, all capabilities dropped, `seccompProfile:
RuntimeDefault`, `automountServiceAccountToken: false` (the Job makes no
Kubernetes API calls — only network calls to the master). `restartPolicy:
OnFailure`, `backoffLimit: 5`, minimal resources (25m/32Mi req, 100m/64Mi lim).

## Replication deviation (documented)

ADR platform/004 (§risks) calls for rack-aware replication. The cluster is
currently **single-node for SeaweedFS** (1 master replica, 1 volume replica per
`projects/platform/seaweedfs/values.yaml`). True rack-aware replication requires
**multiple volume servers**, which do not exist yet. The bucket is therefore
created with **`-replication 000` (no replication)**, which is the correct,
single-node-appropriate setting. **Rack-aware replication is deferred until the
SeaweedFS deployment spans multiple volume servers** — at that point the bucket's
replication policy (and the chart's `volume.replicas` / `defaultReplication`)
should be revisited.

## Deferred / follow-ups

- **Central wiring** of this Application into `projects/platform/kustomization.yaml`
  / `projects/home-cluster/kustomization.yaml` — handled by the orchestrator after
  merge (per unit constraints; this unit must not edit those files).
- **Rack-aware replication** — see deviation above; revisit on multi-node.
- Buckets remain otherwise non-IaC-managed in the repo (existing `knowledge`,
  `trips` buckets were created imperatively); this unit only IaC-manages
  `warehouse`.

## Self-validation

`kubectl kustomize projects/platform/warehouse-bucket/` renders cleanly (single
Job manifest). No Helm here. Manifest was NOT applied and `aws s3 mb` was NOT run
against the cluster — author-only, per unit constraints. (Live kubectl reads to
re-confirm service names/ports were unavailable this session — the cluster API
was unreachable — so the verified discovery facts handed to the unit were used:
`seaweedfs-master.seaweedfs:9333`, `seaweedfs-s3.seaweedfs:8333`, auth disabled.)
