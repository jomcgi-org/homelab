# LIB-ICEBERG — projects/lakehouse/iceberg (Wavefront 2)

**Unit:** LIB-ICEBERG (Wavefront-2 library unit, parallel with LIB-EVENTS,
LIB-NATS, LIB-ORCH, LIB-DUCKDB)
**ADR:** [platform/004 — Iceberg-on-SeaweedFS Lakehouse](../../decisions/platform/004-iceberg-lakehouse-hot-swap.md)
(envelope fields from [agents/017](../../decisions/agents/017-domain-event-schema.md))
**Scope:** purely additive — only new files under `projects/lakehouse/iceberg/`.

## What shipped

The Iceberg **write path** helpers (platform/004 §Write path):

- `iceberg/__init__.py` — package docstring; explains the deliberate envelope
  duplication vs the sibling `events` package (independence).
- `iceberg/catalog.py`
  - `catalog_config(env=None) -> dict` — pure function building the PyIceberg
    catalog config for the SeaweedFS warehouse (S3 endpoint, warehouse root,
    creds, region, path-style). No I/O.
  - `load_warehouse_catalog(env=None)` — thin `load_catalog(...)` wrapper;
    deferred import, not unit-tested (does network/DB I/O).
- `iceberg/writer.py`
  - `rows_to_arrow(rows, schema)` — dict rows -> `pyarrow.Table` via
    `pyiceberg.io.pyarrow.schema_to_pyarrow`.
  - `append_events(table, rows)` — `table.append(rows_to_arrow(...))`; no-op on
    empty; idempotency contract documented (dedup owned upstream).
- `iceberg/tables/__init__.py` — `pkgutil.iter_modules` loader exposing
  `TABLES: dict[str, Schema]` (`table_name -> Schema`). A new domain table is a
  new file exporting `TABLE_NAME` + `SCHEMA`; the loader picks it up.
- `iceberg/tables/note_events.py` — envelope + note payload + per-chunk embedding.
- `iceberg/tables/gap_events.py` — envelope + gap payload (`topic`, `gap_class`,
  `state`).
- Tests: `tables_test.py`, `catalog_test.py`, `writer_test.py` (hermetic; no S3,
  no catalog, no network). 18 tests, all passing locally against a throwaway
  pyiceberg 0.11.1 / pyarrow 24.0.0 install.
- `iceberg/BUILD` — best-effort `py_library(name="iceberg")` (glob, excl
  `*_test.py`, deps `@pip//pyiceberg` + `@pip//pyarrow`) + `py_test` per test +
  `semgrep_test` per test. ci-format-bot normalizes via gazelle.

## Catalog-type decision (+ platform/004 deviation)

platform/004 assumes a **file-based catalog** (the "which snapshot is current"
pointer lives inside the warehouse bucket, so `rclone sync` of the bucket backs
up everything; no external catalog service).

**PyIceberg 0.11.1 has no Hadoop / pure-filesystem catalog.** Available types:
`rest`, `hive`, `glue`, `dynamodb`, `sql`, `in-memory`, `bigquery`. The closest
no-external-service option is **`CatalogType.SQL` (`SqlCatalog`) backed by a
SQLite metadata DB** — `type="sql"`, `uri="sqlite:////warehouse/catalog/...db"`,
`warehouse="s3://warehouse/"`.

**Deviation (follow-up for Wavefront 3):** with SqlCatalog the current-snapshot
pointer lives in a SQLite file, not in the bucket. Iceberg _data_ + _metadata_
files remain immutably in S3 as the ADR describes, but the small catalog index
must be co-located on shared storage and added to the backup set for the ADR's
"clone the bucket, have everything" guarantee to hold. The Wavefront-3
batch-commit deployment must mount the catalog DB on shared SeaweedFS storage and
include it in the backup cron (or revisit if pyiceberg ships a filesystem
catalog). Documented in `catalog.py`'s module docstring.

## SeaweedFS path-style note

The spec asked for `s3.path-style-access: true`. In pyiceberg 0.11.1 the pyarrow
S3 FileIO actually keys off `s3.force-virtual-addressing` (default `True`). We
set **both**: `s3.path-style-access: "true"` (forward-compat / other engines)
**and** `s3.force-virtual-addressing: "false"` (the one pyiceberg reads) so
requests are path-style, which SeaweedFS expects.

## note_events embedding / chunk modeling choice

**One row per chunk.** Each row is a full envelope + note-payload copy plus a
single `embedding` (`list<float>`, 1024-dim voyage-4-nano) and its `chunk_text` /
`chunk_index` / `section_header`. An N-chunk note event produces N rows sharing
`event_id` / `entity_id` / `event_version`; a chunkless event (metadata `updated`,
`tombstoned`) is a single row with `embedding` / `chunk_*` null. Chosen because
the serving HNSW build (platform/004 §serving build) is a flat
`SELECT embedding, note_id, chunk_text ...` with no list `UNNEST`, and vector
search returns the matching chunk + parent note in one row. Cost: envelope
duplication across a note's chunks (well-compressed by Parquet; acceptable at
homelab volume). int8 quantization-at-rest (platform/004 OQ#5) deferred.

## Local verification

Not bazel (per repo policy). Sanity-checked logic against a throwaway
`pyiceberg==0.11.1` / `pyarrow==24.0.0` install: `pytest` 18/18 green. Two
arrow-type facts surfaced and baked into the tests/contract:

- pyiceberg `StringType` -> arrow `large_string`, `ListType` -> arrow
  `large_list` (so tests assert `is_large_string` / `is_large_list`).
- the `timestamptz` `occurred_at` column needs a tz-aware `datetime` row value,
  not an ISO string (`from_pylist` won't coerce str -> timestamp). Producers
  must pass `datetime`.

## Independence

No existing file modified. Does not import the sibling `events` package — the
pyiceberg `Schema` objects standalone-duplicate the ADR-017 envelope fields by
design.
