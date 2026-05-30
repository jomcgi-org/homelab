# INFRA-TEMPORAL — status

**Unit:** INFRA-TEMPORAL · **Status:** shipped (manifests; inert until GitOps wiring)
**Date:** 2026-05-30 · **Branch:** `feat/lakehouse-infra-temporal`
**ADR:** [agents/015 — Temporal as the Orchestration Substrate](../../decisions/agents/015-temporal-orchestration-substrate.md)

---

## What shipped

A purely-additive, path-based ArgoCD Application that deploys Temporal in-cluster
via the upstream `temporalio/temporal` Helm chart, backed by the **existing** CNPG
Postgres cluster `monolith-pg`.

New files (all under a new directory — **no existing files modified**):

| File                                            | Purpose                                                                                       |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------- |
| `projects/platform/temporal/Chart.yaml`         | Wrapper chart; `dependencies: temporal 1.2.0` from `https://go.temporal.io/helm-charts`.      |
| `projects/platform/temporal/application.yaml`   | ArgoCD `Application` (ns `temporal`, sync-wave `2`, automated prune/selfHeal, retry 5).       |
| `projects/platform/temporal/kustomization.yaml` | Aggregator (`resources: [application.yaml]`).                                                 |
| `projects/platform/temporal/values.yaml`        | Postgres backend, replicas, Linkerd opaque ports, SigNoz scrape, OnePasswordItem extraObject. |
| `projects/platform/temporal/BUILD`              | `helm_chart` + `argocd_app` targets (mirrors `nats`/`seaweedfs`).                             |

## Upstream chart

- **Chart:** `temporal`
- **Version:** `1.2.0`
- **appVersion:** `1.31.0` (Temporal server 1.31.0, UI 2.49.1, admin-tools 1.31.0)
- **Helm repo:** `https://go.temporal.io/helm-charts`

## Configuration highlights

- **Backend = existing CNPG `monolith-pg`** (NO new Postgres). Both stores are
  `pluginName: postgres12` against `monolith-pg-rw.monolith.svc.cluster.local:5432`,
  user `app`:
  - default store → DB `temporal`
  - visibility store → DB `temporal_visibility` (standard SQL visibility — **NO Elasticsearch**)
- `createDatabase: false` — the two databases are created out-of-band by the
  sibling **INFRA-CNPG-DBS** unit (CNPG `Database` CRD). `manageSchema: true` — the
  Temporal schema Job (`temporal-sql-tool setup-schema`/`update-schema`) manages the
  tables inside those pre-existing databases. Schema Job runs as a Helm
  `pre-install,pre-upgrade` hook → ArgoCD PreSync, so schema lands before server pods.
- **Replicas:** frontend / history / matching / worker = **2** each; web UI = **1**.
- **Linkerd opaque ports** (meshed cluster, no NetworkPolicies): each server
  component pod carries
  `config.linkerd.io/opaque-ports: "7233,7234,7235,7236,7239,6933,6934,6935,6936,6939"`
  (frontend gRPC 7233 + internode service ports + membership ports); web carries
  `7233`. Schema Job and admintools set `linkerd.io/inject: disabled` (short-lived /
  admin; sidecar would block Job completion).
- **Observability:** Temporal's native Prometheus endpoint (`0.0.0.0:9090`) +
  `prometheus.io/scrape`/`prometheus.io/port: "9090"` pod annotations for SigNoz
  autodiscovery, and the pre-populated `otel.injected-by: kyverno/inject-otel-env-vars`
  annotation (matches the cluster Kyverno mutation → prevents ArgoCD drift, same
  pattern as `seaweedfs`).
- **Internal-only:** all 6 services render as `ClusterIP`; no Ingress, no
  LoadBalancer/NodePort. Per ADR 015 the Temporal API/UI is not externally exposed.
- **Non-root:** `runAsNonRoot: true` + `capabilities.drop: [ALL]` on every component's
  containerSecurityContext; server/admintools keep uid **1000** (see deviation below).

## Validation

```
helm dependency build projects/platform/temporal/   # OK (temporal 1.2.0 fetched)
helm template temporal projects/platform/temporal/ -f projects/platform/temporal/values.yaml -n temporal   # exit 0, 1258 lines, no errors
helm lint projects/platform/temporal/ -f .../values.yaml   # 0 failed (only "icon is recommended")
```

Verified in the rendered output: persistence points at `temporal`/`temporal_visibility`
on `monolith-pg-rw`; `SQL_PASSWORD` resolves to `secretKeyRef{name: temporal-pg, key: password}`;
schema Job emits postgres12 `setup-schema`/`update-schema` (no `create-database`);
replicas 2/2/2/2 + web 1; OnePasswordItem renders into ns `temporal`; all services ClusterIP.

---

## MANUAL PREREQUISITE (must be done before the orchestrator wires Temporal into GitOps)

Temporal runs in the `temporal` namespace but the CNPG-generated `app` password lives
in secret `monolith-pg-app` in the `monolith` namespace. The approved approach
(discovery §6.2) is a **OnePasswordItem** in the `temporal` ns that materialises the
password into a `temporal-pg` Secret. The manifest is in place (`values.yaml`
`extraObjects`), but the 1Password item it points at does not yet exist.

**You (Joe) must create a 1Password item before Temporal can start:**

- **Vault / item:** `vaults/k8s-homelab/items/temporal-pg` (vault `k8s-homelab`, item `temporal-pg`)
- **Required field:** a field named **`password`** containing the **current
  `monolith-pg` `app` password**.
  - Retrieve it from the live secret, e.g.
    `kubectl -n monolith get secret monolith-pg-app -o jsonpath='{.data.password}' | base64 -d`
  - (Do NOT commit it anywhere. This note intentionally does not contain the value.)
- The 1Password Operator will then sync it into a K8s Secret named `temporal-pg` in
  the `temporal` namespace, with key `password` — exactly what the chart's
  `*.sql.existingSecret`/`secretKey` reference.

> If the CNPG `app` password is ever rotated, update the `temporal-pg` 1Password item
> to match, or Temporal will fail to authenticate to Postgres.

---

## Deviations from ADR 015 / task brief

1. **No bundled-DB disabling needed.** The task brief assumed the chart bundles
   cassandra/postgresql/mysql subcharts to disable. The **1.x chart line ships no
   database subcharts** (no `Chart.lock`, no `charts/` deps). We simply configure the
   external SQL stores; there is nothing to turn off. (Older 0.x charts had the
   `cassandra`/`mysql`/`elasticsearch`/`prometheus`/`grafana` subcharts; 1.2.0 does not.)
2. **OTLP export deferred; Prometheus scrape is the shipped path.** Temporal's server
   exports **Prometheus** metrics natively (`:9090`); it does not natively push its own
   metrics/traces over OTLP. We wired the `prometheus.io/scrape` annotations for SigNoz
   autodiscovery (the chart's documented observability path) plus the Kyverno
   `otel.injected-by` annotation. Full OTLP/trace correlation through the Temporal
   **Python SDK** (workflow/activity spans) is a Wavefront 2+ concern (ADR 015 Open
   Question 5) and is the proper place for OTLP wiring — the server-side Prometheus
   scrape is sufficient and idiomatic for the cluster here.
3. **Web UI ingress deferred — ClusterIP only.** Per the brief's fallback: the UI is a
   ClusterIP Service (`temporal-web:8080`). The repo's existing internal-exposure
   patterns are not a drop-in for a gRPC/UI admin surface, so a cluster-internal
   ingress (e.g. an envoy-gateway internal listener / port-forward for admin) is a
   follow-up. ADR 015's hard requirement — **no external/Cloudflare exposure** — is met.
4. **uid 1000, not 65532.** The upstream `temporalio/server`, `temporalio/ui`, and
   `temporalio/admin-tools` images are built to run as uid **1000** with writable paths
   owned by that uid; forcing 65532 breaks them. We keep the chart default uid 1000 but
   enforce `runAsNonRoot: true` + drop ALL capabilities. (Convention is "65532 where the
   chart allows" — this chart does not.)
5. **Single values file.** Mirrors `nats`/`seaweedfs` which use `values.yaml` +
   `values-prod.yaml`, but this unit has a single homelab target, so only `values.yaml`
   (matches the brief's `helm.valueFiles: [values.yaml]`).

## Deferred items

- **OTLP/trace export from the Temporal Python SDK** (workflow/activity spans →
  SigNoz). Owner: Wavefront 2 workflow units. (ADR 015 Open Q5.)
- **Cluster-internal UI ingress** for the Temporal Web UI (currently ClusterIP only).
- **KEDA-driven worker pools** — separate unit (INFRA-KEDA) + Wavefront 4 worker
  Deployments; out of scope here.
- **Sibling dependency:** INFRA-CNPG-DBS must create the `temporal` +
  `temporal_visibility` databases (CNPG `Database` CRD) at sync-wave 1 before this app
  (wave 2) starts.

## GitOps wiring (deferred to orchestrator)

These manifests are **inert** until the orchestrator adds
`projects/platform/temporal` to `projects/platform/kustomization.yaml` `resources:` and
regenerates `projects/home-cluster/kustomization.yaml`. That central wiring is handled
**after** this PR merges. Intended and correct.
