# INFRA-CNPG-DBS — Declarative Temporal databases on monolith-pg

**Unit:** INFRA-CNPG-DBS · **Status:** complete · **Classification:** [purely-additive]
**Date:** 2026-05-30 · **Author:** lakehouse subagent (INFRA-CNPG-DBS)

Creates the two PostgreSQL databases Temporal needs — `temporal` (core/persistence
store) and `temporal_visibility` (visibility store) — on the existing CNPG cluster
`monolith-pg` (namespace `monolith`), both owned by role `app`. Declarative, additive,
GitOps-managed.

---

## What was built

New directory `projects/platform/temporal-databases/` (4 new files, zero existing
files modified):

| File                                | Purpose                                                                                  |
| ----------------------------------- | ---------------------------------------------------------------------------------------- |
| `database-temporal.yaml`            | CNPG `Database` CR for the `temporal` database.                                          |
| `database-temporal-visibility.yaml` | CNPG `Database` CR for the `temporal_visibility` database.                               |
| `kustomization.yaml`                | Lists the two Database CRs as `resources` (raw kustomize, no Helm).                      |
| `application.yaml`                  | ArgoCD `Application` (plain kustomize source, destination ns `monolith`, sync-wave `1`). |

## Database CRD — apiVersion + exact spec fields used

- **apiVersion:** `postgresql.cnpg.io/v1`
- **kind:** `Database` (scope: **Namespaced** — the CR MUST live in the same namespace
  as the `Cluster` it references; here `monolith`).
- **Operator version:** CloudNativePG **v1.28.1** (Helm chart `cloudnative-pg` `0.27.1`,
  `appVersion: 1.28.1`, per `projects/platform/cloudnative-pg/deploy/application.yaml`).
- **Spec fields used** (validated against the CNPG v1.28.1 CRD — its `spec.required` is
  `[cluster, name, owner]`):
  - `spec.cluster.name: monolith-pg` — reference to the hosting CNPG Cluster.
  - `spec.name` — the **PostgreSQL** database name. This is the field name in v1.28.1
    (NOT `databaseName`). Immutable. Values: `temporal` and `temporal_visibility`.
  - `spec.owner: app` — maps to `CREATE DATABASE ... OWNER`. Role `app` is the bootstrap
    owner role of the monolith cluster, so both DBs are owned by `app`.
  - `spec.ensure: present` — explicit (it is also the CRD default).

Note the metadata/PG-name split for the visibility DB: the CR `metadata.name` is
`temporal-visibility` (DNS-safe, hyphen) while `spec.name` is `temporal_visibility`
(the actual PG database name, underscore — what Temporal connects to).

## Why the Database CRD over postInitSQL

`postInitSQL` runs only **once**, at cluster bootstrap, and editing it means modifying
the existing `Cluster` resource in `projects/monolith/chart/templates/cnpg-cluster.yaml`
— which is **out of scope** for this additive unit and would not retroactively create
DBs on the already-bootstrapped cluster anyway. The declarative `Database` CRD is the
approved path: it is purely additive (new CRs in new files), the CNPG operator
reconciles the DBs onto the live cluster, and the desired state stays in Git.

## ArgoCD Application notes

- **destination.namespace: `monolith`** — required, because the Database CRs are
  namespaced and must co-locate with the `monolith-pg` Cluster.
- **No `CreateNamespace=true`** — the `monolith` namespace already exists (owned by the
  monolith app); creating it here would be wrong.
- **prune + selfHeal enabled** is safe: an ArgoCD Application only tracks/prunes the
  resources it itself renders (the two Database CRs). It does **not** adopt or prune
  other resources living in the `monolith` namespace.
- **`ServerSideApply=true`** is set so applying these CRs into the foreign-owned
  `monolith` namespace uses field-managed SSA and never clobbers manifests owned by the
  monolith Application.
- **sync-wave `1`** so the databases reconcile before the Temporal app (sync-wave `2`,
  sibling unit) bootstraps.
- **`app.kubernetes.io/part-of: shared-infrastructure`** label, matching the existing
  platform Application convention (`nats`, `seaweedfs`).

## Caveat — two ArgoCD Applications touching the `monolith` namespace

After wiring, both the `monolith` Application and this `temporal-databases` Application
will operate in the `monolith` namespace. This is benign because each only manages its
own rendered resources, but it is the one thing to keep in mind if pruning behaviour
ever looks surprising. The Database CRs (`metadata.name` `temporal` /
`temporal-visibility`) do not collide with any monolith-managed resource names.

`databaseReclaimPolicy` defaults to `retain`, so deleting a Database CR will NOT drop
the underlying PostgreSQL database — intentional, to avoid accidental data loss.

## Deferred (out of scope, handled centrally by the orchestrator after merge)

- Wiring this Application into `projects/platform/kustomization.yaml` and the
  auto-generated `projects/home-cluster/kustomization.yaml`.
- Temporal Helm deployment (sibling unit) that consumes these databases.
- Verifying the operator reconciled the DBs on the live cluster (cluster API was not
  reachable from the build workstation; schema validated against the CNPG v1.28.1 CRD
  definition, which is the exact CRD the installed operator ships).

## Self-validation

- `kubectl kustomize projects/platform/temporal-databases/` renders both Database CRs
  cleanly (offline; no API server required).
- Spec fields validated against the CNPG v1.28.1 `databases.postgresql.cnpg.io` CRD:
  required `[cluster, name, owner]` all present; `ensure` is a valid enum (`present`).
- All four YAML files parse.
- Nothing applied to the cluster; no bazel test run.
