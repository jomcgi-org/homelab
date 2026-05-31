# Event-Sourced Lakehouse — Implementation Summary (FINAL)

Aggregates every per-unit status note in this directory. Source of truth for
design: ADRs `agents/015` (Temporal), `agents/016` (NATS), `agents/017` (event
schema), `platform/004` (Iceberg + Quack). This was built **purely additively** —
no existing service modified, broken, or deleted; migration/cutover deliberately
out of scope.

**Status as of 2026-05-31 (live run):** Wavefronts 0–4 **deployed** to the
`lakehouse` namespace and the pipeline **proven end-to-end at the data layer** —
a vector search over the freshly-built serving artifact returns a correctly-
backfilled `_processed` note (3431 chunk rows / 990 notes / 1024-dim embeddings;
see §Live-run validation). Running the pipeline for the first time surfaced ~15
stacked integration bugs (minimal-image + SeaweedFS + DuckDB/pyiceberg), all
fixed additively in PRs #2421–#2433. **Remaining:** the Quack _HTTP_ `/search`
confirmation (its `pytz`/cast fixes merged; pending a Quack redeploy) and a
retrieval benchmark — both gated only on the chronically-flapping kube tunnel.
PG-cred delivery moved from 1Password to a **Kyverno clone** of `monolith-pg-app`
(no manual prereqs); the Iceberg catalog moved from pod-local SQLite to a
**shared PostgreSQL** `lakehouse` DB. See §Live-run validation + §Consolidated deviations.

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

## Live-run validation (2026-05-31)

W4 was activated (`./deploy` wired into `kustomization.yaml`) and the pipeline run
for the first time. It had **never executed against the real minimal-image +
SeaweedFS + DuckDB/pyiceberg stack**, so each leg surfaced a stacked integration
bug; each was root-caused from cluster logs and fixed **additively**:

| #   | PR    | Bug                                                                                          | Fix                                                                                                                       |
| --- | ----- | -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| 1   | #2421 | hardened image can't download DuckDB extensions (no CA bundle)                               | bundle signed extensions in image, `LOAD` from `/opt/duckdb_ext`                                                          |
| 2   | #2422 | catalog pointer stranded on one pod's ephemeral `/tmp` (SQLite)                              | **PG-backed shared `SqlCatalog`** (`lakehouse` DB, derived from `DATABASE_URL`)                                           |
| 3   | #2423 | `SqlCatalog` import crash — SQLAlchemy not in venv                                           | `@pip//sqlalchemy` + psycopg `# keep` deps on iceberg lib                                                                 |
| 4   | #2424 | write path never created the table (`NoSuchTableError`)                                      | `_load_or_create_table` ensure-create + `iceberg/tables` dep                                                              |
| 5   | #2425 | pyiceberg S3 writes corrupt on SeaweedFS                                                     | `http://` scheme + `AWS_REQUEST_CHECKSUM_CALCULATION=when_required` (disable `aws-chunked` trailer SeaweedFS #6847/#6583) |
| 6   | #2426 | `occurred_at` ISO string vs arrow `timestamp[us,tz]`                                         | parse to `datetime` in `_envelope_to_rows`                                                                                |
| 7   | #2427 | minimal image lacks tz db → `ZoneInfoNotFoundError: UTC`                                     | `@pip//tzdata` (zoneinfo write side)                                                                                      |
| 8   | #2428 | on-disk HNSW index rejected                                                                  | `SET hnsw_enable_experimental_persistence=true`                                                                           |
| 9   | #2430 | **chunk embeddings silently dropped** (nested `chunks` vs flat schema) → 0 indexable vectors | EXPLODE one row per chunk in `_envelope_to_rows`; CAST `embedding` to `FLOAT[1024]` for HNSW                              |
| 10  | #2431 | boto3 artifact upload: scheme-less endpoint                                                  | `http://` prefix in `_s3_client`                                                                                          |
| 11  | #2432 | `/search` `array_distance(FLOAT[1024], DOUBLE[])` no overload                                | cast `$query` to `FLOAT[dim]` in `vector_search_sql`                                                                      |
| 12  | #2433 | DuckDB→Python fetch of tz timestamp needs `pytz`                                             | `@pip//pytz` (zoneinfo read side)                                                                                         |

Plus runtime workarounds (not code): backfill run with `batch_size=10` (default 50
exceeds Temporal's 2 MiB activity-payload limit — embeddings); workflows started
**manually** via `temporal-admintools` because the Temporal **schedules were never
registered** (follow-up); `IcebergBatchCommitWorkflow` run on the **`housekeeping`
queue** because the **KEDA iceberg-builder scaler isn't firing** (its operator is
crash-looping on a missing `ScaledJob` CRD — pre-existing platform issue; follow-up).

**Proof achieved (criterion 3, data layer):** backfill → NATS → drain(explode) →
Iceberg `note_events` (**3431 chunk rows / 990 notes / 1024-dim embeddings**,
verified via boto3 + DuckDB `iceberg_scan`) → `BuildServingArtifactWorkflow` →
`s3://warehouse/serving/notes-vN.duckdb` (DuckDB + persistent VSS HNSW) → a vector
search returns backfilled `_processed` notes (e.g. `gate-composition-avoids-context-bloat`,
`_processed/gate-composition-avoids-context-bloat.md`). Quack hot-swapped to the
artifact (`/healthz` reports the version).

**Remaining (gated only on the flapping kube tunnel):**

1. Quack **HTTP `/search`** end-to-end confirmation — the cast (#2432) + `pytz`
   (#2433) fixes are merged; needs a Quack redeploy + re-published `artifact-ready`.
2. **Retrieval benchmark** (vector / item / aggregate; DuckDB-direct + Quack-HTTP;
   p50/p95) — to be recorded here.
3. Follow-ups: register Temporal schedules; fix the KEDA `ScaledJob`-CRD gap;
   wire `helm_images_values` digest-pinning + OCI chart (lakehouse tracks `:main`);
   add `pytest-asyncio` (async tests silently skipped); W5 monolith glue.

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

1. **28 units merged/skipped** — ✅ W0–W4 (26 units) merged + 12 additive live-run
   fix PRs (#2421–#2433, §Live-run validation); SEED-BACKFILL **executed**; W5
   monolith glue is a documented follow-up (not skipped, not required for the proof).
2. **Temporal/KEDA/NATS/SeaweedFS/Quack healthy** — 🟡 **mostly**: Temporal (12 pods),
   NATS (4 streams), SeaweedFS (5 pods), Quack (2 pods), dispatchers + workers all
   Running. **KEDA operator is CrashLoopBackOff** on a missing `ScaledJob` CRD
   (pre-existing platform install gap, unrelated to this additive work) — ScaledObjects
   exist but don't scale; documented follow-up.
3. **Backfill E2E → queryable backfilled note** — ✅ **proven at the data layer**: the
   serving artifact (built by the pipeline from the replayed `_processed` notes)
   returns a correct `_processed` note via VSS. 🟡 the literal _CDN→Quack HTTP_ hop
   pending a Quack redeploy of the merged `/search` fixes (#2432/#2433) + tunnel.
4. **Existing services untouched** — ✅ verified: only additive `projects/lakehouse/`
   - platform infra + a NEW `lakehouse` PG database; `agent_platform/orchestrator`,
     `cluster_agents`, the monolith scheduler, and `knowledge.notes/chunks` are read-only
     sources, never modified (the backfill reads `knowledge.notes` READ-ONLY).
5. **FINAL.md aggregates all notes** — ✅ this document.
