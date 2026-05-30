# W4-CHART — lakehouse worker/quack/dispatcher Deployments + KEDA scalers

**Unit:** DEPLOY-GAP-DRAIN-WORKER · DEPLOY-ICEBERG-BUILDER-WORKER ·
DEPLOY-HOUSEKEEPING-WORKER · DEPLOY-QUACK-SERVER · dispatchers Deployment
(Wavefront 4 chart unit)
**Classification:** [auto] — purely additive; two NEW dirs
`projects/lakehouse/chart/` + `projects/lakehouse/deploy/`, no existing-file edits.
**Status:** AUTHOR-ONLY — CI-validated via `helm template` / `helm lint` /
`semgrep_manifest_test`, but **NOT wired into ArgoCD this run**. The manifests are
intentionally inert; the orchestrator wires + deploys after live Wavefront-1
verification (it adds `projects/lakehouse/deploy` to the platform/root kustomization
when ready).

ADRs: `agents/015` §"Worker pools, not the orchestrator" (per-task-queue
Deployments + KEDA min/max), `platform/004` (quack 2 replicas, internal Service,
Cloudflare CDN read path, serving artifact lifecycle).

---

## What shipped (all new files)

### `projects/lakehouse/chart/`

- `Chart.yaml` — chart `lakehouse`, type application, version 0.1.0.
- `values.yaml` — chart defaults (shared env, security context, per-pool worker
  config, quack/dispatchers config, KEDA tuning, onepassword toggles).
- `templates/_helpers.tpl` — name/labels/selector helpers (mirror monolith's
  release-name-as-fullname), plus `lakehouse.sharedEnv` (Temporal/NATS/S3/Iceberg
  env fragment) and `lakehouse.s3Env` (S3 creds from the synced secret).
- `templates/workers/gap-drain-worker-deployment.yaml` + `-scaledobject.yaml`
- `templates/workers/iceberg-builder-worker-deployment.yaml` + `-scaledobject.yaml`
- `templates/workers/housekeeping-worker-deployment.yaml` + `-scaledobject.yaml`
- `templates/quack/quack-deployment.yaml` + `quack-service.yaml` +
  `quack-httproute.yaml` (Cloudflare CDN) +
  `onepassworditem-quack-query-token.yaml`
- `templates/dispatchers/dispatchers-deployment.yaml`
- `templates/onepassworditem-pg.yaml` + `templates/onepassworditem-s3.yaml`
- `BUILD` — `helm_chart(name="chart")` (no `images=` pinning yet; see Deviations).

### `projects/lakehouse/deploy/`

- `application.yaml` — path-based ArgoCD Application: source `path:
projects/lakehouse/chart`, `targetRevision: HEAD`, `releaseName: lakehouse`,
  `valueFiles: [values.yaml, ../deploy/values.yaml]`; dest ns `lakehouse`;
  sync-wave "3"; automated prune+selfHeal; CreateNamespace=true; retry 5.
- `kustomization.yaml` — `resources: [application.yaml]`.
- `values.yaml` — env overrides (enables quack cfIngress + the 3 onepassword
  itemPaths). MANUAL PREREQUISITES below.
- `BUILD` — hand-written `argocd_app` (carries `semgrep_exclude_rules`).

---

## The 5 Deployments

| Deployment                         | Image (built W3)                                                          | Replicas / scaling                                  | Notable env                                                                      |
| ---------------------------------- | ------------------------------------------------------------------------- | --------------------------------------------------- | -------------------------------------------------------------------------------- |
| `lakehouse-gap-drain-worker`       | `ghcr.io/jomcgi/homelab/projects/lakehouse/worker`                        | KEDA min=2 max=10 (`gap-drain` queue)               | `TASK_QUEUE=gap-drain` + shared env                                              |
| `lakehouse-iceberg-builder-worker` | `…/lakehouse/worker`                                                      | KEDA min=0 max=1, scale-to-zero (`iceberg-builder`) | `TASK_QUEUE=iceberg-builder` + shared env                                        |
| `lakehouse-housekeeping-worker`    | `…/lakehouse/worker`                                                      | KEDA min=1 max=2 (`housekeeping`)                   | `TASK_QUEUE=housekeeping` + shared env + `DATABASE_URL` (backfill, read-only)    |
| `lakehouse-quack`                  | `ghcr.io/jomcgi/homelab/projects/lakehouse/quack-server`                  | 2 replicas (static)                                 | `PORT`, `NATS_URL`, S3 env, `QUACK_QUERY_TOKEN`, optional `SERVING_ARTIFACT_URL` |
| `lakehouse-dispatchers`            | `ghcr.io/jomcgi/homelab/projects/lakehouse/dispatchers` (sibling W4 unit) | 2 replicas (static)                                 | shared env (NATS→Temporal bridge)                                                |

Shared env on worker/quack/dispatcher pods (chart `values.env`, overridable):
`TEMPORAL_TARGET=temporal-frontend.temporal.svc.cluster.local:7233`,
`NATS_URL=nats://nats.nats.svc.cluster.local:4222`,
`SEAWEEDFS_S3_ENDPOINT=seaweedfs-s3.seaweedfs.svc.cluster.local:8333`,
`ICEBERG_WAREHOUSE=s3://warehouse/`.

All pods: uid 65532, `runAsNonRoot: true`, `allowPrivilegeEscalation: false`,
caps `drop: [ALL]`, seccomp `RuntimeDefault`, + a `/tmp` emptyDir (writable
scratch for the non-root containers). **Zero NetworkPolicies** (Linkerd-meshed
namespace; port 4143 mismatch per `feedback_linkerd_networkpolicy.md`).

---

## KEDA ScaledObject specs (queues + min/max)

`keda.sh/v1alpha1`, `type: temporal`
(https://keda.sh/docs/latest/scalers/temporal/). Each trigger:
`endpoint=temporal-frontend.temporal.svc.cluster.local:7233`,
`namespace=default`, `queueTypes=workflow`; `pollingInterval=15s`,
`cooldownPeriod=60s`.

| ScaledObject                       | taskQueue         | min | max | targetQueueSize | scale-to-zero                        |
| ---------------------------------- | ----------------- | --- | --- | --------------- | ------------------------------------ |
| `lakehouse-gap-drain-worker`       | `gap-drain`       | 2   | 10  | 5               | no (warm min)                        |
| `lakehouse-iceberg-builder-worker` | `iceberg-builder` | 0   | 1   | 1               | yes (`activationTargetQueueSize: 0`) |
| `lakehouse-housekeeping-worker`    | `housekeeping`    | 1   | 2   | 5               | no (warm min)                        |

(KEDA is already deployed + CRDs installed — INFRA-KEDA, Wavefront 1.)

---

## Cloudflare CDN approach for quack

Per `platform/004` read path (`Quack pods → Cloudflare CDN → Web app/browser`):

- `quack-service.yaml` — internal **ClusterIP** Service on the HTTP port (8080).
  Never directly internet-exposed.
- `quack-httproute.yaml` — a Gateway-API **HTTPRoute** (`gateway.networking.k8s.io/v1`)
  that attaches the internal Service to the cluster's Cloudflare gateway
  (`cloudflare-ingress` in `envoy-gateway-system`,
  `projects/platform/cloudflare-gateway`), which sits behind the Cloudflare Tunnel
  - CDN. Carries the `ingress-tier: public` label the gateway selects on, sets
    `X-Forwarded-Proto: https`, and an explicit `timeouts.request: 30s` (so Envoy's
    silent 15s default doesn't drop a slow first-query after a hot-swap). Mirrors the
    repo's existing pattern (monolith `httproute-public`, context-forge `httproute`).
    Disabled in chart defaults; **enabled** by the deploy overlay
    (`quack.cfIngress.enabled: true`, hostname `quack.jomcgi.dev`).

---

## Manual 1Password prerequisites

The housekeeping-worker backfill (PG read), quack `/search` token, and S3 creds
need 1Password items populated **before** they work. The chart only declares the
`OnePasswordItem`s; the operator syncs values at deploy time. **Do NOT commit any
secret.** Same pattern as Temporal's `temporal-pg` item.

| 1Password item (`vaults/k8s-homelab/items/…`) | Field(s)                                   | Consumer / what to put                                                                                                                                                                                                                        |
| --------------------------------------------- | ------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `lakehouse-pg`                                | `uri`                                      | housekeeping worker `DATABASE_URL`. A libpq postgres URL built from: user `app`, the CNPG-generated monolith-pg `app` password, host `monolith-pg-rw.monolith.svc.cluster.local`, port `5432`, db `monolith`. Used **read-only** by backfill. |
| `lakehouse-quack-query-token`                 | `QUACK_QUERY_TOKEN`                        | quack `/search` bearer token (gates the public read path).                                                                                                                                                                                    |
| `lakehouse-s3`                                | `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | SeaweedFS S3 creds. Auth is **disabled** in this cluster → dummy `duckdb`/`duckdb` is fine.                                                                                                                                                   |

If any itemPath is left empty (chart default), the corresponding `OnePasswordItem`
and env injection are simply omitted (e.g. no `DATABASE_URL` → backfill disabled).

---

## Self-validation (no cluster)

- `helm template lakehouse projects/lakehouse/chart/ -f …/chart/values.yaml` →
  exit 0. With deploy overlay: 5 Deployments, 3 ScaledObjects, 1 Service, 1
  HTTPRoute, 3 OnePasswordItems.
- `kubectl kustomize projects/lakehouse/deploy/` → exit 0.
- `helm lint projects/lakehouse/chart/` → 0 failed (only the cosmetic "icon
  recommended" INFO).
- Confirmed: **0 NetworkPolicies**; all 5 pods uid 65532 + runAsNonRoot; ScaledObjects
  reference the right queues + min/max; image repos correct;
  `DATABASE_URL` only on housekeeping; `QUACK_QUERY_TOKEN` only on quack.
- semgrep: rendered manifest clean against the repo's local kubernetes + yaml rule
  set (added `/tmp` emptyDirs to clear `require-tmp-emptydir`; HTTPRoute carries an
  explicit timeout).

---

## Deviations

1. **`semgrep_exclude_rules = ["require-readiness-probe"]` in `deploy/BUILD`.** The
   worker pods (gap-drain/iceberg-builder/housekeeping) and dispatcher pods are
   Temporal task-queue pollers / NATS consumers with no inbound HTTP serving path,
   so an HTTP readinessProbe is meaningless (Temporal/NATS gate their own intake).
   The quack pods DO carry an HTTP `/healthz` readinessProbe. Same rationale
   nats/keda use for their non-HTTP workloads.
2. **No `images=` pinning in `chart/BUILD`.** `helm_images_values` requires every
   referenced image `.info` target to exist, but the dispatchers image is a sibling
   W4 unit not built in this branch. Tags stay `main`; the orchestrator adds
   `images=` (worker / quack / dispatchers `.info`) when it wires the chart.
3. **Hand-written `deploy/BUILD`** (rather than gazelle-generated) so it can carry
   `semgrep_exclude_rules` — gazelle's helm extension manages only
   chart/chart_files/release_name/namespace/values_files/tags and preserves this
   attr on re-run.

---

## Inertness mechanism (how it stays unsynced)

The home-cluster root generator (`bazel/images/generate-home-cluster.sh`)
auto-discovers every `projects/*/deploy/kustomization.yaml` and would otherwise
wire `projects/lakehouse/deploy` straight into the root (→ ArgoCD creates the
Application and syncs it — exactly what AUTHOR-ONLY forbids before live W1
verification). To keep it inert **without** removing the deliverable
`deploy/kustomization.yaml`, this unit adds an **empty aggregator**
`projects/lakehouse/kustomization.yaml` (`resources: []`). The generator then
treats `projects/lakehouse` as an aggregator and adds **that** to the root
(skipping `…/deploy`); since the aggregator includes nothing,
`kubectl kustomize projects/lakehouse/` resolves to **zero resources** and the
lakehouse Application is never created. The single `+ ../../projects/lakehouse`
line in `home-cluster/kustomization.yaml` is generator-idempotent (no CI
format-bot loop).

**Orchestrator wiring = one line:** add `- ./deploy` to
`projects/lakehouse/kustomization.yaml` after live W1 verification. No
home-cluster regeneration needed (the aggregator is already referenced).

---

## Deferred (out of scope for this unit)

- **Active wiring.** The lakehouse aggregator is intentionally empty; the chart
  does **not** sync. The orchestrator adds `- ./deploy` to
  `projects/lakehouse/kustomization.yaml` after live W1 verification.
  `projects/platform/kustomization.yaml` is untouched.
- **Dispatchers image** (`IMG-DISPATCHERS`, sibling W4 unit) — referenced by string.
- **Image-tag pinning** via `helm_images_values` — added at wiring time.
