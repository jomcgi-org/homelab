# Wavefront 0 — Discovery

**Unit:** WAVEFRONT-0-discover · **Status:** complete · **Classification:** [manual-review]
**Date:** 2026-05-30 · **Author:** lakehouse /goal runner

Source of truth for design intent: ADRs `agents/015`, `agents/016`, `agents/017`,
`platform/004`. This document records **what already exists vs. what must be built**,
verified against the **live cluster** (context `ssh-homelab`) and the repo at
`origin/main` (`9b02d2fee`). Wavefront 1 agents read this to decide whether their unit
is needed, a no-op, or needs re-scoping before dispatch.

---

## 1. Executive summary

| Capability                       | State                                                                                     | Implication for the plan                                                                                                                                                              |
| -------------------------------- | ----------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **NATS JetStream**               | ✅ deployed (`nats` ns, `nats-0` 4/4, file store, single node, 50Gi PVC)                  | **Reuse.** No NATS operator → streams created via `nats` CLI / init Job, **not** CRDs. Only stream today is `ais` (2M msgs); **no `events.*` streams** — create fresh, zero conflict. |
| **SeaweedFS**                    | ✅ deployed (`seaweedfs` ns, master/volume/filer/s3 all up)                               | **Reuse.** Buckets today: `knowledge` (empty), `trips` (27 GB). **No `warehouse` bucket.** Buckets are _not_ IaC-managed → create `warehouse` idempotently via init Job/CLI.          |
| **CNPG Postgres**                | ✅ `monolith-pg` healthy (`monolith` ns, 1 instance)                                      | **Reuse instance.** DBs present: `monolith`, `postgres` only. `temporal`/`temporal_visibility` **absent**. `Database` CRD (`postgresql.cnpg.io`) **is installed**, 0 CRs exist.       |
| **Temporal**                     | ❌ absent (no `temporal` ns)                                                              | **Build.** Full Helm deploy via `temporalio/helm-charts`.                                                                                                                             |
| **KEDA**                         | ❌ absent (no `keda` ns, no `scaledobjects` CRD, no pods)                                 | **Build.** Full deploy.                                                                                                                                                               |
| **Quack server**                 | ❌ absent                                                                                 | **Build.** New image + chart.                                                                                                                                                         |
| **`_processed` backfill corpus** | ✅ 4585 notes ready                                                                       | Well-defined, bounded, all live, all embedded. See §4.                                                                                                                                |
| **Existing services to protect** | `agent_platform/orchestrator`, `cluster_agents`, monolith scheduler, `knowledge.*` tables | All present & untouched. Backfill reads `knowledge.notes/chunks` **read-only**.                                                                                                       |

**Bottom line:** Three of the six infra pieces already exist (NATS, SeaweedFS, CNPG
instance). The build surface is Temporal, KEDA, Quack, the stream/bucket/DB _config_
on existing infra, and the entire monolith-side Python/workflow/worker/dispatcher
layer. **Four plan assumptions are wrong in ways that change unit classification and
file layout — see §6. These are the gate decisions.**

---

## 2. Live component inventory (verified)

### Namespaces (relevant)

`nats` (75d), `seaweedfs` (155d), `monolith` (61d), `cnpg-system` (61d),
`agent-platform` (80d), `cluster-agents` (82d), `inference` (37d), `argocd`,
`linkerd`, `signoz`. **No** `temporal`, **no** `keda`. (`todo` ns is Terminating —
unrelated.)

### NATS (`nats` ns)

- `nats-0` 4/4 Running (47d), `nats-box` 2/2 (75d). JetStream **enabled**, file
  store `/data`, 50Gi longhorn PVC, **single replica** (`cluster.enabled: false`),
  pinned to `node-4`. Linkerd opaque-port `4222`.
- Chart: `projects/platform/nats/` wraps upstream `nats` `2.12.3` (path-based
  Application, `targetRevision: HEAD`).
- **Streams today:** `ais` only (AIS position reports, 2,046,555 msgs, 782 MiB).
  **No `events.knowledge.*`, `events.serving.*`, `events.ingest.*`, `events.ops.*`.**
- CLI access: `kubectl exec -n nats -c nats-box deploy/nats-box -- nats --server nats://nats:4222 …`
  (**must pass `-c nats-box`** — default container is `linkerd-proxy`).

### SeaweedFS (`seaweedfs` ns)

- master/volume/filer/s3 all 2/2 Running. S3 gateway port 8333, auth disabled
  (cluster-internal). `enableSecurity: false`.
- Chart: `projects/platform/seaweedfs/` wraps upstream `seaweedfs` (path-based,
  `targetRevision: HEAD`).
- **Buckets:** `knowledge` (size 0), `trips` (27 GB). **No `warehouse`.** No bucket
  IaC in the repo → must create `warehouse` via init Job or `weed shell` (idempotent).
- S3 endpoint (internal): `http://seaweedfs-s3.seaweedfs.svc.cluster.local:8333`.

### CNPG (`monolith-pg`, `monolith` ns)

- `Cluster monolith-pg` healthy, **1 instance**, primary `monolith-pg-1`. Defined in
  `projects/monolith/chart/templates/cnpg-cluster.yaml`; bootstrap database `monolith`,
  owner `app`, `postInitSQL: CREATE EXTENSION vector`.
- Databases present: `monolith`, `postgres`. `temporal`, `temporal_visibility` **absent**.
- **`Database` CRD `databases.postgresql.cnpg.io` is installed** (created 2026-03-30),
  **0 CRs** — so additive declarative DB creation is possible (see §6.3).
- App secret: **`monolith-pg-app`** (`kubernetes.io/basic-auth`, 11 keys — CNPG
  standard set: `uri`, `username`, `password`, `dbname`, `host`, `port`, `user`,
  `jdbc-uri`, `pgpass`, …). RW service: `monolith-pg-rw.monolith.svc.cluster.local:5432`.
- Note: single-instance, no replica. Temporal sharing this instance matches ADR 015
  Open Question 1 ("shared is fine; revisit if throughput pressures").

### Temporal / KEDA / Quack

- All absent. Greenfield builds.

---

## 3. Repo conventions (captured for downstream units)

### 3.1 Two ArgoCD deploy patterns — **decides auto-merge eligibility**

- **Path-based (platform infra).** `projects/platform/<svc>/` = `Chart.yaml`
  (`dependencies:` → upstream Helm repo) + `application.yaml`
  (`source.path: projects/platform/<svc>`, `targetRevision: HEAD`,
  `helm.valueFiles: [values.yaml, values-prod.yaml]`). **ArgoCD reads the chart from
  git** → changes need **no OCI version bump**. → can be pure-new-file → **auto-merge**.
  Examples: `nats`, `seaweedfs`, `kyverno`.
- **OCI-version-based (monolith).** `deploy/application.yaml` uses `sources:` —
  chart pulled from `ghcr.io/jomcgi/homelab/charts` at `targetRevision: 0.86.3` (an
  immutable OCI tag), plus a `$values` git ref. **Every chart change requires bumping
  `chart/Chart.yaml` version AND `deploy/application.yaml` targetRevision** (two
  existing-file edits) → **cannot** be pure-new-file → **manual-review**.

### 3.2 ArgoCD app discovery

- Root `projects/home-cluster/kustomization.yaml` is **auto-generated** by
  `bazel/images/generate-home-cluster.sh` (scans `projects/*/kustomization.yaml`
  aggregators + `projects/*/deploy/kustomization.yaml`).
- A new `projects/platform/<svc>/` must be added to
  `projects/platform/kustomization.yaml` `resources:` (existing-file edit), **then**
  regenerate the root. A new top-level `projects/<svc>/deploy/` is auto-discovered on
  regen without editing the platform kustomization.
- **Consequence:** units adding a new `projects/platform/<svc>/` touch
  `projects/platform/kustomization.yaml` + regenerate `home-cluster/kustomization.yaml`
  → not strictly new-file-only. A new **top-level** `projects/<svc>/deploy/` only needs
  the regen of the generated root file. (See §6 for handling.)

### 3.3 sync-wave convention

`0` = policy/secret operators (kyverno, onepassword); `1` = core infra
(nats, seaweedfs); `2` = controllers consuming infra; unannotated = after. New infra
(temporal, keda, warehouse) → wave `1`; dependent workers/dispatchers → `2`+.

### 3.4 1Password secrets

`kind: OnePasswordItem` (`onepassword.com/v1`), `spec.itemPath:
vaults/<vault>/items/<item>`; operator syncs to a K8s Secret named `metadata.name`,
consumed via `env[].valueFrom.secretKeyRef`. Per-namespace (the Secret lands in the
OnePasswordItem's namespace).

### 3.5 Monolith Python package layout (**plan path correction**)

- Modules are **top-level packages** under `projects/monolith/` with `imports = ["."]`:
  `from knowledge.models import …`, `from shared.embedding import …`. There is **no
  `monolith` top-level package** — so the plan's `projects/monolith/monolith/events/`
  path is **wrong**.
- `projects/monolith/BUILD` is **hand-written** and `# gazelle:exclude`s all 10 module
  dirs (`agent chat app e2e knowledge notes scripts home scheduler shared`). The image
  - binary + every `py_test` are hand-registered there.
- **Gazelle runs repo-wide** (`@rules_python_gazelle_plugin//python`,
  `map_kind py_binary py_venv_binary`). A **new, unexcluded** package under
  `projects/monolith/` (e.g. `events/`) would get **gazelle-auto-generated BUILD
  files** that the format hook auto-commits — likely wrong for this repo's custom
  `py_test`/`@pip//` conventions, and a shared touchpoint. (See §6.1.)
- DB access: `projects/monolith/app/db.py`, **sync** SQLAlchemy/SQLModel,
  `DATABASE_URL` env (`postgresql://…` → rewritten to `postgresql+psycopg://`).
- Embedding: `shared/embedding.py`, model **`voyage-4-nano`**, **1024-dim** vectors,
  via `EMBEDDING_URL` `/v1/embeddings`.

### 3.6 Standalone Python project template

`projects/stargazer/` and `projects/ships/` are standalone Python services:
`projects/<svc>/{backend,chart,deploy,tests}` with **per-directory gazelle-managed
BUILD files** and a **path-based** ArgoCD Application. This is the idiomatic template
for net-new code that should **not** ride the monolith chart's OCI release cadence.

### 3.7 apko image build

`//bazel/tools/oci:py3_image.bzl` (`py3_image`) and `apko_image.bzl`. Backend image:
`py3_image(name="image", binary="//projects/monolith:main", main="app/main.py",
multiarch_tars=[…], repository="ghcr.io/jomcgi/homelab/projects/monolith/backend")`.
apko yaml: Wolfi repos, `archs: [x86_64, aarch64]`, `run-as: 65532`. Non-root uid
65532 throughout, `runAsNonRoot: true`, `capabilities.drop: [ALL]`.

### 3.8 Linkerd / NetworkPolicy

All target namespaces are Linkerd-meshed. **No NetworkPolicies** (per
`feedback_linkerd_networkpolicy.md` — port 4143 mismatch). NATS uses Linkerd
opaque-port `4222`; Temporal frontend gRPC (`7233`) will likely need an opaque-port
annotation too.

---

## 4. `_processed` backfill corpus (WF-BACKFILL / SEED-BACKFILL scope)

`_processed/` is the vault directory of fully-distilled notes; `knowledge.notes` holds
one row per `.md` under `/vault/_processed` (`path` like `_processed/<slug>.md`).

| Metric                                        | Value                   |
| --------------------------------------------- | ----------------------- | ----- |
| `_processed` notes (path LIKE `_processed/%`) | **4585**                |
| …live (`deleted_at IS NULL`)                  | **4585** (all live)     |
| …with ≥1 chunk (embeddings present)           | **4585** (all embedded) |
| Total notes (all paths)                       | 6278 (5656 live)        |
| Total chunks                                  | 18489                   |
| Gaps                                          | 1119 · Note-links       | 31991 |

**`knowledge.notes` columns** (schema `knowledge`): `id` (int PK), `note_id` (stable
slug identity = natural `entity_id`), `path`, `title`, `content_hash`, `type`
(atom/active/…), `status`, `visibility` (public/private/null), `visibility_verified`,
`source`, `tags[]`, `aliases[]`, `created_at`, `updated_at`, `extra` (JSONB),
`indexed_at`, `layout_*`, `deleted_at`, `pre_delete_path`.

**`knowledge.chunks`:** `note_fk → notes.id`, `chunk_index`, `section_header`,
`chunk_text`, `embedding Vector(1024)`.

**Sample rows:** `wong-zakai-theorem` (`_processed/wong-zakai-theorem.md`, type atom,
public); `standby-as-reliability-crutch-anti-pattern` (atom, public);
`shipit-v2-monolith-kargo-project` (type/status active).

**Backfill mapping → ADR 017 envelope:** `entity_type="note"`, `entity_id=note_id`,
`event_type="created"`, `event_version=1`, `Nats-Msg-Id = {note_id}-v1` (idempotent /
re-runnable), subject `events.knowledge.note`. `payload` carries note metadata + chunk
texts + **pre-computed voyage-4-nano embeddings** (so serving rebuilds never re-embed,
per platform/004 §"What does NOT change"). **Read path = raw `SELECT` via psycopg**
(no monolith model import — keeps the worker image lean and avoids the
`monolith_backend` dep graph).

---

## 5. Per-unit disposition for Wavefront 1

| Unit                   | Disposition                     | Notes                                                                                                                                                                                               |
| ---------------------- | ------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **INFRA-TEMPORAL**     | **BUILD**                       | New `projects/temporal/deploy/` (top-level, path-based). Backend = `monolith-pg` (`temporal`/`temporal_visibility` DBs). Needs cross-ns PG creds (§6.2). Skip ES; OTLP→SigNoz; internal UI ingress. |
| **INFRA-KEDA**         | **BUILD**                       | Absent. New deploy. Provides `scaledobjects.keda.sh` CRD that Wavefront 4 needs.                                                                                                                    |
| **INFRA-SEAWEEDFS**    | **CONFIG (additive)**           | SeaweedFS deployed; only create `warehouse` bucket (idempotent init Job / `weed shell`). **Do not** modify `projects/platform/seaweedfs/values.yaml`.                                               |
| **INFRA-NATS-STREAMS** | **CONFIG (additive)**           | NATS deployed, **no operator** → create `events.*` streams via init Job running `nats stream add` (idempotent), **not** CRDs. Infinite/file retention per ADR 016.                                  |
| **INFRA-CNPG-DBS**     | **BUILD (additive, re-scoped)** | Use `Database` CRD (installed) as new manifests in ns `monolith`, owner `app`. **Not** `postInitSQL` (that edits the existing Cluster). See §6.3.                                                   |
| **DOCS-EVENT-BUS**     | **BUILD**                       | New `docs/event-bus.md`. Pure new file → auto-merge.                                                                                                                                                |

Wavefront 1 touches **zero** monolith Python/Bazel — it is all deploy YAML + one docs
file. The §6.1 layout decision can therefore be ratified at the **Wavefront 1→2 gate**
without blocking Wavefront 1.

---

## 6. Plan deviations requiring a decision (gate items)

> Per the goal: deviations the ADR didn't anticipate get surfaced, not silently
> applied. These four are the substantive ones.

### 6.1 Where does the new monolith-side code live? (affects Wavefronts 2–4)

The plan says `projects/monolith/monolith/{events,nats_client,orchestrator,…}/`. That
path **does not exist** and would (a) be imported wrong, (b) trigger gazelle to
auto-generate BUILD files for unexcluded new dirs, (c) require editing
`projects/monolith/BUILD` (shared touchpoint) to register the worker image + tests.

- **Option A (recommended): new standalone project `projects/lakehouse/`** (stargazer
  pattern). Sub-packages `events/ nats_client/ orchestrator/ iceberg/ duckdb_query/
dispatchers/` under it; per-dir gazelle-managed BUILD; worker + quack images and a
  **path-based** chart all new-file. Reuse `knowledge`/`shared` either via a
  `//projects/monolith:monolith_backend` Bazel dep **or** (preferred for the backfill)
  raw SQL. Keeps Wavefronts 2–4 pure-new-file/auto-merge; no monolith chart/BUILD edits.
- **Option B: `projects/monolith/<pkg>/` top-level packages** + add each to
  `# gazelle:exclude` and hand-write BUILD (edits monolith BUILD = shared touchpoint;
  serializes those units) + worker image as a new `py3_image` in monolith BUILD (edit)
  - monolith chart version bumps for worker Deployments (manual-review).
- **Option C: hybrid** — libraries in `projects/lakehouse/`, but workers deployed via
  the monolith chart. Worst of both (OCI coupling + cross-project dep).

**Recommendation: Option A.** It is the only option that preserves the plan's
"new-file-only, auto-merge, conflict-free" property. Import paths become
`from lakehouse.events import …` (one-line correction to every Wavefront 2 unit).

### 6.2 Temporal needs Postgres creds cross-namespace

`monolith-pg-app` lives in `monolith` ns; Temporal runs in `temporal` ns. Options:
(a) a `OnePasswordItem` in `temporal` ns holding the `app` password (decouples from
CNPG's generated secret), (b) a secret-replication mechanism, (c) run Temporal in the
`monolith` ns (rejected — ADR 015 wants a dedicated `temporal` ns). **Recommend (a)**;
INFRA-TEMPORAL implementer wires `temporalio` chart `*.sql.existingSecret` to it.

### 6.3 INFRA-CNPG-DBS: Database CRD, not postInitSQL

The CRD is installed and additive (new manifest, ns `monolith`, `spec.cluster.name:
monolith-pg`, `owner: app`). `postInitSQL` would edit the existing
`cnpg-cluster.yaml` (out-of-scope file). **Open sub-question:** which Application syncs
the two `Database` CRs without touching the monolith chart? Recommend a **tiny new
top-level `projects/temporal-databases/deploy/`** (or fold into `projects/temporal/`)
as new files. Plan's `[auto-merge]` holds **only** if it avoids the monolith chart.

### 6.4 Worker-Deployment units (Wavefront 4) auto-merge classification

The plan marks DEPLOY-\* `[auto-merge]` assuming new template files under
`projects/monolith/chart/templates/workers/`. Under the monolith chart that's false
(OCI version bump = existing-file edits → manual-review). **Resolved by Option A**:
deploy workers via the standalone `projects/lakehouse/` chart (path-based) → genuinely
new-file → `[auto-merge]` preserved.

---

## 7. Anti-pattern guardrails confirmed clear

No PG read models proposed (backfill is read-only of existing tables; new state lives
in events/Iceberg). No custom DLQ/claim/heartbeat (Temporal). No NetworkPolicies. No
agent_platform edits. No SearXNG/web-tool wiring. No OCI-cadence tightening (hot-swap).
No agent-substrate/AX vendoring.

## 8. Verified-untouched (success criterion 4 baseline)

`agent-platform` + `cluster-agents` namespaces Active; `agent_platform/orchestrator/`
& `cluster_agents/` dirs present; monolith `scheduler/` module present;
`knowledge.{notes,chunks,gaps,note_links,…}` tables present with the row counts in §4.
Re-run these `kubectl get` + row-count queries at the end to prove no drift.

---

## 9. Gate ask (Wavefront 0 → Wavefront 1)

Confirm before Wavefront 1 dispatches:

1. **Proceed** to Wavefront 1 (Temporal, KEDA, SeaweedFS-bucket, NATS-streams,
   CNPG-DBs, docs) given the above.
2. **Ratify §6.1 Option A** (`projects/lakehouse/` standalone project) as the home for
   all Wavefront 2–4 code — or pick B/C. (Needed before Wavefront 2; can be deferred to
   the W1→W2 gate, but confirming now lets W1 name the namespace/Applications
   consistently.)
3. **Confirm §6.2 / §6.3 recommendations** (OnePasswordItem for Temporal PG creds;
   Database CRD via a new Application).
