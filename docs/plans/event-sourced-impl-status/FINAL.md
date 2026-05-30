# Event-Sourced Lakehouse — Implementation Summary (FINAL)

Aggregates every per-unit status note in this directory. Source of truth for
design: ADRs `agents/015` (Temporal), `agents/016` (NATS), `agents/017` (event
schema), `platform/004` (Iceberg + Quack). This was built **purely additively** —
no existing service modified, broken, or deleted; migration/cutover deliberately
out of scope.

**Status as of this writing:** Wavefronts 0–4 (26 units) merged, CI-green, and
**inert** (nothing deployed). The cluster-dependent remainder — W1 live health
verification, W4 deployment/activation, W5 monolith glue, and the end-to-end
backfill proof — is **blocked on the kube API tunnel** (down for the duration of
the build) and two manual 1Password prerequisites. See §Pending + §Runbook.

---

## Status by wavefront

| Wavefront                  | Units                                                                                                                                             | PRs         | State                                                            |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ----------- | ---------------------------------------------------------------- |
| **0 — discover**           | WAVEFRONT-0-discover                                                                                                                              | #2385       | ✅ merged                                                        |
| **1 — infra**              | INFRA-TEMPORAL, INFRA-KEDA, INFRA-SEAWEEDFS (warehouse bucket), INFRA-NATS-STREAMS (nack + 4 Stream CRs), INFRA-CNPG-DBS, DOCS-EVENT-BUS + wiring | #2386–#2392 | ✅ merged; **not yet verified live**                             |
| **2 — libraries**          | LIB-EVENTS, LIB-NATS, LIB-TEMPORAL, LIB-ICEBERG, LIB-DUCKDB-QUERY (+ LIB-SCAFFOLD)                                                                | #2393–#2398 | ✅ merged, CI-green                                              |
| **3 — workflows + images** | WF-GAP-DRAIN, WF-BACKFILL, WF-ICEBERG-BATCH, WF-BUILD-SERVING, WF-TAG-ROTATION, WF-SCHEDULES, IMG-WORKER, IMG-QUACK-SERVER (+ W3-PREP)            | #2399–#2404 | ✅ merged; both images pushed to GHCR                            |
| **4 — deployments**        | DEPLOY-{GAP-DRAIN,ICEBERG-BUILDER,HOUSEKEEPING}-WORKER, DEPLOY-QUACK-SERVER, SVC-DISPATCHERS, INFRA-SEAWEEDFS-LIFECYCLE                           | #2406–#2408 | ✅ merged, **AUTHOR-ONLY / inert** (chart not wired into ArgoCD) |
| **5 — glue + seed**        | GLUE-MONOLITH-STARTUP, GLUE-NEW-API-ENDPOINTS, SEED-BACKFILL, FINAL-AGGREGATOR                                                                    | —           | ⏳ FINAL ✅ (this doc); glue + seed **pending tunnel**           |

Each row links to its per-unit note in this directory for the full ship/deviation log.

---

## What was built (architecture as realized)

```
monolith PG (knowledge.notes/_processed, READ-ONLY)
   │  BackfillFromProcessedNotesWorkflow (housekeeping worker, raw SQL)
   ▼
NATS JetStream  events.knowledge.note  (nack-managed Stream CRs, infinite retention)
   │  IcebergBatchCommitWorkflow (iceberg-builder worker, every ~90s)
   ▼
Iceberg on SeaweedFS  s3://warehouse/  (note_events / gap_events tables)
   │  BuildServingArtifactWorkflow (housekeeping worker, every 15min)
   ▼
s3://warehouse/serving/notes-vN.duckdb  (DuckDB + VSS HNSW, state=building)
   │  events.serving.artifact-ready  →  TagRotationWorkflow (5min grace, keep-last-24)
   ▼
Quack pods (2×, in-RAM .duckdb, ATTACH OR REPLACE hot-swap)  →  Cloudflare CDN  →  query
```

- **Code home:** standalone `projects/lakehouse/` project (gate-ratified Option A) —
  path-based chart, gazelle-managed BUILD, imports as `projects.lakehouse.*`. Zero
  edits to the monolith chart/BUILD; W2–W4 stayed pure-new-file/auto-merge.
- **Orchestration:** Temporal (chart 1.2.0 / app 1.31.0) on the existing CNPG
  `monolith-pg` (new `temporal` + `temporal_visibility` DBs via the CNPG `Database`
  CRD — additive, no Cluster edit). KEDA 2.19.0 scales worker pools on Temporal
  queue depth (gap-drain 2–10; iceberg-builder 0–1 scale-to-zero; housekeeping 1–2).
- **Idempotency:** three stacked layers — `Nats-Msg-Id = {entity_id}-v{version}`
  JetStream dedup → deterministic Temporal workflow IDs → consumer fold to latest
  `event_version`. The backfill is re-runnable by construction.
- **Backfill corpus:** 4585 live `_processed` notes, voyage-4-nano 1024-dim
  embeddings carried in the event payload (no re-embedding on serving rebuild).

---

## Pending (blocked on the kube tunnel + manual prerequisites)

**Two manual prerequisites** (user, in 1Password — values are CNPG-generated so
can't be committed):

1. `vaults/k8s-homelab/items/temporal-pg` — field `password` = monolith-pg `app`
   password (`kubectl -n monolith get secret monolith-pg-app -o jsonpath='{.data.password}' | base64 -d`). Temporal pods crashloop until present.
2. `vaults/k8s-homelab/items/lakehouse-pg` (`uri`), `lakehouse-quack-query-token`,
   `lakehouse-s3` (dummy `duckdb`/`duckdb` while SeaweedFS S3 auth is off).

**Cluster tunnel** (`127.0.0.1:6443`) must be restored for every step below.

---

## Runbook — activate + verify + prove (once tunnel + 1Password are ready)

1. **Verify Wavefront 1 healthy** (criterion 2 + 4 baseline):
   `kubectl get pods -n temporal -n keda -n nats -n seaweedfs`; Temporal UI loads;
   `kubectl get scaledobjects -A` (KEDA CRD); NATS streams (`nats stream ls` via
   `-c nats-box`) show `events.{knowledge,serving,ingest,ops}`; `warehouse` bucket
   exists; `temporal`+`temporal_visibility` DBs exist (`\l` on monolith-pg). Confirm
   `agent_platform/orchestrator`, `cluster_agents`, monolith scheduler, and
   `knowledge.*` tables are **unchanged** (same `kubectl get` + row counts as §discover).
2. **Activate W4:** one-line `- ./deploy` into `projects/lakehouse/kustomization.yaml`
   (currently empty aggregator). ArgoCD syncs the `lakehouse` namespace; KEDA brings
   up the worker pools, quack (2×), dispatchers.
3. **Register schedules + monolith glue (W5 GLUE):** author + deploy
   GLUE-MONOLITH-STARTUP (register Temporal Schedules on boot via
   `orchestrator.schedules.register_schedules`; additively publish events to NATS
   in the monolith's existing mutation txn — outbox) and GLUE-NEW-API-ENDPOINTS.
   These modify the monolith ([manual-review]) — verify live before/after.
4. **Run the backfill (W5 SEED-BACKFILL, criterion 3):**
   `temporal workflow start --type BackfillFromProcessedNotesWorkflow --task-queue housekeeping ...`
   on the housekeeping worker. Observe: events → `events.knowledge.note`;
   IcebergBatchCommitWorkflow grows snapshots; BuildServingArtifactWorkflow writes a
   serving artifact; artifact-ready hot-swaps Quack; a query through Cloudflare→Quack
   returns a correctly-backfilled `_processed` note.
5. **Finalize:** update this doc with the live verification + seed results.

---

## Consolidated deviations from the ADRs / plan

- **Code path corrected** plan's `projects/monolith/monolith/…` → standalone
  `projects/lakehouse/` (gate-ratified). Imports `projects.lakehouse.*`.
- **NATS streams via nack CRs** (user choice) rather than an init Job.
- **CNPG `Database` CRD** for temporal DBs (additive) rather than `postInitSQL`.
- **Cross-namespace PG creds** for Temporal + the backfill worker via OnePasswordItem
  (manual population) — the CNPG `app` password is operator-generated.
- **PyIceberg SqlCatalog (SQLite metadata)** — pyiceberg 0.11.1 has no filesystem
  catalog; the SQLite index must be co-located on shared storage + backed up (the
  ADR's "clone the bucket, have everything" needs that addendum). [LIB-ICEBERG]
- **SeaweedFS lifecycle: prefix-based TTL** (`weed fs.configure -ttl=1d` on
  `serving/`) not tag-filtered expiry — SeaweedFS 3.73 S3 lifecycle is unreliable;
  build-workflow keep-last-24 is the backstop. [INFRA-SEAWEEDFS-LIFECYCLE]
- **SeaweedFS replication 000** (single volume node) — rack-aware deferred to
  multi-node. [INFRA-SEAWEEDFS]
- **Temporal OTLP + internal UI ingress** shipped as Prometheus-scrape + ClusterIP;
  full OTLP/ingress are documented follow-ups. [INFRA-TEMPORAL]
- **gap-drain is shadow/parallel only** — definitions + harness-invoking activity
  skeleton built per ADR 015, but production cutover from the live orchestrator is
  explicitly out of scope. [WF-GAP-DRAIN, SVC-DISPATCHERS]
- **gazelle module manifest** left stale; new pip deps resolved via per-package
  `# gazelle:resolve` directives (proven) rather than a risky manifest regen.

## Out of scope (valid future runs — NOT done here)

Deletion of `agent_platform/orchestrator` & `cluster_agents`; migration of existing
scheduled jobs to Temporal; cutover of any read path PG→Quack; Pi + DeepSeek Flash
via NIM; SearXNG/web-tool MCP wiring.

## Acceptance criteria

1. **28 units merged/skipped** — ✅ W0–W4 (26 units) merged; W5 glue + seed pending
   tunnel (documented here, not skipped).
2. **Temporal/KEDA/NATS/SeaweedFS/Quack healthy** — ⏳ pending live verification (tunnel).
3. **Backfill E2E via CDN→Quack** — ⏳ pending (tunnel; see Runbook §4).
4. **Existing services untouched** — ✅ code-side verified (no out-of-scope file
   touched across all PRs); ⏳ runtime confirmation pending tunnel.
5. **FINAL.md aggregates all notes** — ✅ this document.
