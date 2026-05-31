"""DuckDB + Iceberg-extension query helpers for the lakehouse serving/read paths.

This package implements the DuckDB side of ADR platform/004
(Iceberg-on-SeaweedFS lakehouse with hot-swap Quack serving):

  * **Workflow read path** — Temporal workers query Iceberg directly on S3 via the
    DuckDB ``iceberg`` + ``httpfs`` extensions (~100ms warm, 1-2min fresh).
  * **Hot-swap serving** — Quack pods hold an in-RAM ``.duckdb`` artifact with a
    pre-built HNSW (VSS) index, swapped zero-downtime via ``ATTACH OR REPLACE``
    from S3 (verified non-blocking on DuckDB 1.5.3).

Design split (important for hermetic CI):

  * **Pure SQL builders** (``s3_secret_sql``, ``attach_or_replace_sql``,
    ``vector_search_sql``) return SQL strings and touch no network — these are
    what the unit tests exercise.
  * **Extension/S3-touching helpers** (``load_extensions``, ``connect`` with a
    remote artifact) LOAD the signed DuckDB extensions bundled into the image at
    ``/opt/duckdb_ext`` (no network) and read S3. These are kept out of the
    default test path so hermetic CI never touches the on-disk bundle.

SeaweedFS S3 auth is currently disabled, so the configured KEY_ID/SECRET may be
dummy values; the secret is still required for the DuckDB ``httpfs`` S3 layer to
construct request signatures.
"""

from projects.lakehouse.duckdb_query.query import (
    attach_or_replace_sql,
    connect,
    load_extensions,
    s3_secret_sql,
    vector_search_sql,
)

__all__ = [
    "attach_or_replace_sql",
    "connect",
    "load_extensions",
    "s3_secret_sql",
    "vector_search_sql",
]
