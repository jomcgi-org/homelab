# WF-STORAGE — storage-path Temporal workflows (Wavefront-3)

**Unit:** WF-STORAGE (Wavefront-3 workflow unit)
**Source of truth:** [ADR platform/004](../../decisions/platform/004-iceberg-lakehouse-hot-swap.md)
(§Write path, §Serving artifact build, §Serving artifact lifecycle, §build cadence, §Risks)
**Convention:** [W3-PREP.md](W3-PREP.md) — flat workflow files under
`orchestrator/workflows/`, each exporting module-level `WORKFLOWS` / `ACTIVITIES`.

## What shipped (all new files under `orchestrator/workflows/`)

Three workflow modules + a hermetic test each:

- `iceberg_batch.py` — `IcebergBatchCommitWorkflow` + `drain_and_commit` activity.
- `build_serving.py` — `BuildServingArtifactWorkflow` + `build_artifact` activity.
- `tag_rotation.py` — `TagRotationWorkflow` + `rotate_tags` activity.
- `iceberg_batch_test.py`, `build_serving_test.py`, `tag_rotation_test.py`.

All I/O lives in `@activity.defn` functions; workflow bodies are deterministic
(no wall-clock except `workflow.now()`, no NATS/S3/DuckDB calls). Heavy third-party
libs (`nats`, `pyiceberg`, `boto3`, `duckdb`) are imported **inside** the activity
bodies so Temporal's workflow sandbox never loads them — `imports_passed_through()`
was therefore unnecessary.

## Key design decisions

### IcebergBatchCommitWorkflow — drain → commit → ack (at-least-once)

- One durable pull consumer (`iceberg-batch-commit`) over the wildcard subject
  `events.knowledge.*` (covers note/gap/edge/future entities with one consumer).
- Per batch: validate each `EventEnvelope`, group rows by target Iceberg table via
  `TABLE_BY_ENTITY` (`note`→`note_events`, `gap`→`gap_events`), `append_events`
  one commit per table, then **ack only after the commit succeeds**.
- **Idempotency contract (documented in the module):** delivery is at-least-once;
  the ack-after-commit ordering means a crash between commit and ack redelivers
  and re-appends (duplicate raw rows), which is tolerated because dedup is owned
  upstream — NATS `Nats-Msg-Id = {entity_id}-v{version}` (layer 1), this workflow's
  commit-then-ack (layer 2), and readers folding to the latest `event_version` per
  entity. Exactly-once would need a transactional NATS-ack-with-Iceberg-commit that
  neither system offers; at-least-once + idempotent read fold is the correct,
  simpler choice.
- Unmapped entity types are **skipped and left un-acked** (redeliver + surface for
  ops) rather than silently dropped.
- The workflow loops `drain_and_commit` until a fetch is empty, then returns (the
  WF-SCHEDULES cadence re-triggers it); a `max_drains_per_run` cap forces
  `continue_as_new` so a busy stream can't grow one run's history unboundedly.

### BuildServingArtifactWorkflow — HNSW build + current-version filter

- `build_artifact` opens a DuckDB connection (`duckdb_query.connect`), builds the
  serving `chunks` table from the latest `note_events` Iceberg snapshot
  (`iceberg_scan`), builds a **VSS HNSW index** over `embedding` with `metric='l2sq'`
  (matching `duckdb_query.vector_search_sql`'s `array_distance` ordering), persists
  a local `.duckdb`, uploads to `s3://warehouse/serving/notes-v{N}.duckdb` tagged
  `state=building`, then publishes `events.serving.artifact-ready`.
- **Current-version filter (stale-vector mitigation, platform/004 §Risks):** the
  build folds the append-only event log to current state **before** indexing —
  keep each note's `MAX(event_version)` row(s), drop `tombstoned` notes, drop
  null-embedding rows. The HNSW therefore indexes only live, current chunks, so a
  vector hit can never return a stale/deleted note. Applied at build time (rows
  excluded from the index entirely) rather than the ADR's per-query hash-join
  filter — cheaper and stronger.
- **Build cadence is a runtime knob:** 15min initial cadence is owned by
  WF-SCHEDULES; the workflow is cadence-agnostic (always builds the latest
  snapshot), so tightening the schedule needs no code/infra change.
- Version is derived deterministically from `workflow.now().timestamp()` (monotonic
  across the cadence, no external counter).
- `events.serving.artifact-ready` is published on an **explicit subject** (not in
  `SUBJECT_BY_ENTITY`) so it routes to the Quack swap consumer, not back into the
  Iceberg drainer.

### TagRotationWorkflow — lifecycle state machine + retention

- Workflow body: durable `workflow.sleep(5min)` grace (lets in-flight queries
  finish on the old `current`), then the `rotate_tags` activity.
- `rotate_tags`: demote existing objects one step (`current`→`previous`,
  `previous`→`stale`) **before** promoting the just-built artifact to `current`
  (never two `current`s at once), via boto3 `PutObjectTagging` against the SeaweedFS
  S3 endpoint. Belt-and-suspenders **keep-last-N=24** sweep force-stales anything
  beyond the newest 24 by version (catches gaps the SeaweedFS lifecycle daemon
  misses).

## Tests (hermetic — no Temporal test server, no network)

Per the spec, `temporalio.testing.WorkflowEnvironment` is avoided (it downloads a
server binary). Tests exercise the **activity functions directly** with
AsyncMock/MagicMock for `NatsClient` / iceberg catalog / DuckDB connection / boto3,
asserting grouping + ack-after-commit, the current-version-filter + HNSW SQL shape,
the `state=building` tag, the artifact-ready publish, and the tag-rotation state
machine + retention. Each module's workflow class is asserted to be a
`@workflow.defn` (`temporalio.workflow._Definition.from_class`) present in
`WORKFLOWS`, and each activity present in `ACTIVITIES`.

## Deviations

- **One BUILD edit (deviates from W3-PREP "touch no BUILD file").** `build_artifact`
  / `rotate_tags` need `boto3` for S3 object **tagging** (`PutObjectTagging`) — the
  `building→current→previous→stale` lifecycle has no DuckDB/PyIceberg equivalent
  (neither can set object tags). `boto3` is a declared+locked pip dep but was NOT in
  the `workflows/BUILD` `_WORKFLOW_DEPS` superset, and pyiceberg 0.11.1 does not pull
  it transitively. Per the convention this is the "STOP and flag" case; resolved by
  adding the single additive line `@pip//boto3` to the superset with an explanatory
  comment. Collision-free in practice: WF-STORAGE is the only workflow unit that
  touches S3 directly, so no sibling competes for this dep.
- **Iceberg namespace seam closed locally.** The W2 `iceberg.tables` modules define
  only leaf `TABLE_NAME`; the namespace was an open seam. platform/004 names the
  hierarchy `warehouse.knowledge.*`, so this unit uses namespace `knowledge`
  (`ICEBERG_NAMESPACE`, env-overridable) for `catalog.load_table((ns, table))` and
  the `iceberg_scan` source URI. WF-DOMAIN's table-creation must use the same
  namespace; the env knob keeps them in sync.
- **Drain subject is a wildcard** (`events.knowledge.*`) rather than enumerating
  `SUBJECT_BY_ENTITY` values — one durable consumer covers all current and future
  entity types. A test asserts every `TABLE_BY_ENTITY` key is a known publish
  subject.

## Verification

No local `bazel test` (repo convention — no darwin workflows runners). All six files
AST-parse + `py_compile` clean; `ruff format` + `ruff check` pass; no
`\.svc\.cluster\.local` literal (semgrep `no-hardcoded-k8s-service-url`) and no
flagged inline stdlib imports (`no-inline-stdlib-import`). Relying on PR-branch CI
for `bazel test //...` + `ci-format-bot` BUILD/format normalization.
