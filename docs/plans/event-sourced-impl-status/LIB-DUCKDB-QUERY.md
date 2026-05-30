# LIB-DUCKDB-QUERY — DuckDB + Iceberg-extension query helpers

**Unit:** LIB-DUCKDB-QUERY (Wavefront 2, parallel library unit)
**ADR:** [platform/004 — Iceberg-on-SeaweedFS lakehouse with hot-swap Quack serving](../../decisions/platform/004-iceberg-lakehouse-hot-swap.md)
**Branch:** `feat/lakehouse-lib-duckdb`

## What shipped

Purely additive, new files only under `projects/lakehouse/duckdb_query/`:

- `__init__.py` — package docstring + re-exports of the public surface.
- `query.py` — DuckDB query helpers split into a pure layer and a network layer.
- `query_test.py` — hermetic pytest (pure builders + extension-free in-memory smoke).
- `BUILD` — best-effort `py_library` (`duckdb_query`) + `py_test` (`query_test`);
  `ci-format-bot` normalizes and adds the `semgrep_test` targets.

No existing file was modified. The unit imports no sibling lakehouse package.

## Pure-SQL-builder vs extension-loading split

The central design constraint: loading DuckDB extensions (`httpfs`/`iceberg`/`vss`)
downloads them from the internet at runtime, which fails in hermetic CI. So the
module is two layers and **tests only touch the pure layer**:

**Pure SQL builders (unit-tested, no side effects, no network):**

- `s3_secret_sql(env=None) -> str` — `CREATE OR REPLACE SECRET ... TYPE S3` for the
  SeaweedFS endpoint. `SEAWEEDFS_S3_ENDPOINT` (default
  `seaweedfs-s3.seaweedfs.svc.cluster.local:8333`), `URL_STYLE 'path'`,
  `USE_SSL false`, `REGION 'us-east-1'`, KEY_ID/SECRET from `S3_ACCESS_KEY_ID`/
  `S3_SECRET_ACCESS_KEY` (default dummy `duckdb`/`duckdb`, since SeaweedFS auth is off).
- `attach_or_replace_sql(alias, path) -> str` — hot-swap
  `ATTACH OR REPLACE '<path>' AS <alias> (READ_ONLY);` (platform/004 §hot-swap).
- `vector_search_sql(table, k) -> str` — VSS HNSW NN template
  (`ORDER BY array_distance(embedding, $query) LIMIT k`); validates `k` is a positive
  int (interpolated into LIMIT, so checked rather than bound). `$query` is bound at
  execute time.

**Network / extension-touching helpers (NOT called by tests):**

- `load_extensions(con)` — `INSTALL`/`LOAD` httpfs, iceberg, vss (network on install).
- `connect(*, read_only_artifact=None, env=None)` — `duckdb.connect()` →
  `load_extensions` → `s3_secret_sql` → optional `ATTACH OR REPLACE` of the serving
  artifact as schema `notes`. Documents the hot-swap ATTACH pattern.

The in-memory smoke test uses `duckdb.connect(':memory:')` and asserts `SELECT 42`
returns 42 with no extensions loaded.

## platform/004 deviations / notes

- `attach_or_replace_sql` adds `(READ_ONLY)`: serving pods never mutate the artifact
  (the builder workflow rewrites it whole), and read-only attach is the safe default
  for the serving read path. Not contradicted by the ADR; an implementation choice.
- `vector_search_sql` assumes only an `embedding` column on the target table; the
  HNSW index that makes the `array_distance` ORDER BY use the index is created by the
  serving-artifact build (Wavefront 3), as the ADR specifies. The template is
  index-agnostic and works (slower, full scan) without it.
- The "current-version filter" hash-join (ADR Risks: stale vector hits) is left to the
  artifact build / query composition in later wavefronts — this unit ships the base
  NN template only.
- DuckDB pinned to 1.5.3 in the lock (`@pip//duckdb`), matching the version
  platform/004 verified for ATTACH OR REPLACE hot-swap semantics.

## Gazelle manifest gap (cross-unit note)

First CI `Format check` failed: gazelle could not resolve `import duckdb` because
`duckdb` (and `pyarrow`, `pyiceberg`, `temporalio`) are **missing from
`bazel/tools/python/gazelle_python.yaml`** — LIB-SCAFFOLD added them to the
requirements lock but did not regenerate that manifest. Rather than edit the shared
manifest (collision-prone across the 5 parallel units), this unit stays independent
via a `# gazelle:resolve py duckdb @pip//duckdb` directive in its BUILD. Sibling
units that import `pyarrow`/`pyiceberg`/`temporalio` will hit the same gap and need
either their own resolve directive or a serialized manifest regen.

Also dropped the `TYPE_CHECKING` `from duckdb import DuckDBPyConnection` in favour of
the module-qualified `duckdb.DuckDBPyConnection` annotation (string-evaluated under
`from __future__ import annotations`), so gazelle only sees the top-level
`import duckdb` and the single resolve directive covers everything.

## Semgrep `no-hardcoded-k8s-service-url` exclusion

CI semgrep flagged `DEFAULT_S3_ENDPOINT = "seaweedfs-s3.seaweedfs.svc.cluster.local:8333"`
under the `no-hardcoded-k8s-service-url` rule. The unit spec **requires** this exact
default (endpoint "from `SEAWEEDFS_S3_ENDPOINT` (default `seaweedfs-s3...:8333`)"), and
the env override is present — which is precisely the mitigation the rule guards against
(release-rename drift). Resolved per the documented pattern: a
`# gazelle:semgrep_exclude_rules no-hardcoded-k8s-service-url` directive plus
`exclude_rules = ["no-hardcoded-k8s-service-url"]` on the `semgrep_test` targets
(`# nosemgrep` is not honored by these targets). This is a query helper, not a Go
application default, so the override-then-default shape is appropriate.

## Status

Implemented. See PR on `feat/lakehouse-lib-duckdb`. pytest (4 tests) passed on the
first Test run; only the semgrep target needed the exclusion above.
