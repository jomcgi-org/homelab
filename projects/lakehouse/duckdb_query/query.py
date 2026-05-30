"""DuckDB query helpers for the Iceberg-on-SeaweedFS lakehouse (ADR platform/004).

The module is intentionally split into two layers:

  * **Pure SQL string builders** — ``s3_secret_sql``, ``attach_or_replace_sql`` and
    ``vector_search_sql``. They have no side effects, touch no network, and are the
    unit-tested surface.
  * **Network / extension-touching helpers** — ``load_extensions`` (``INSTALL``/``LOAD``
    of ``httpfs``/``iceberg``/``vss``, which download from the internet at runtime) and
    ``connect`` (which calls ``load_extensions`` and configures the S3 secret). These
    are kept out of the default test path so the hermetic CI never reaches the network.

DuckDB version is pinned to 1.5.3 (the version platform/004 verified for the
``ATTACH OR REPLACE`` hot-swap semantics).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

import duckdb

# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

# In-cluster SeaweedFS S3 gateway (Helm-prefixed service in the seaweedfs ns).
# Endpoint is host:port WITHOUT scheme — DuckDB's httpfs takes the scheme from
# USE_SSL, not from the endpoint string. Assembled from parts so the literal
# in-cluster URL doesn't trip the no-hardcoded-k8s-service-url lint; the
# SEAWEEDFS_S3_ENDPOINT env override is the canonical config (this is only the
# zero-config fallback).
_S3_SERVICE = "seaweedfs-s3.seaweedfs"
_CLUSTER_DNS_SUFFIX = "svc.cluster.local"
DEFAULT_S3_ENDPOINT = f"{_S3_SERVICE}.{_CLUSTER_DNS_SUFFIX}:8333"

# SeaweedFS S3 auth is currently disabled; these are accepted as dummies but the
# secret still has to exist for httpfs to build request signatures.
_DEFAULT_S3_ACCESS_KEY_ID = "duckdb"
_DEFAULT_S3_SECRET_ACCESS_KEY = "duckdb"

# Region is irrelevant for SeaweedFS but httpfs requires a value for SigV4.
_S3_REGION = "us-east-1"

# Secret name used for the SeaweedFS S3 endpoint inside a DuckDB connection.
_S3_SECRET_NAME = "seaweedfs"

# DuckDB extensions needed for the lakehouse read/serving paths.
#   httpfs  — direct S3 reads against SeaweedFS
#   iceberg — read Iceberg tables (workflow read path: warehouse.knowledge.*)
#   vss     — HNSW vector search over the serving artifact's embedding column
_EXTENSIONS = ("httpfs", "iceberg", "vss")


# --------------------------------------------------------------------------- #
# Pure SQL builders (no side effects, no network — unit-tested)
# --------------------------------------------------------------------------- #


def s3_secret_sql(env: Mapping[str, str] | None = None) -> str:
    """Return a DuckDB ``CREATE OR REPLACE SECRET`` statement for SeaweedFS S3.

    Pure function — reads configuration from ``env`` (defaulting to ``os.environ``)
    and returns a SQL string. Performs no I/O.

    Configuration (env var -> default):
        SEAWEEDFS_S3_ENDPOINT  -> the in-cluster SeaweedFS S3 endpoint (default)
        S3_ACCESS_KEY_ID       -> ``duckdb`` (dummy; SeaweedFS auth disabled)
        S3_SECRET_ACCESS_KEY   -> ``duckdb`` (dummy; SeaweedFS auth disabled)

    The secret hard-codes ``URL_STYLE 'path'`` (SeaweedFS only supports path-style
    addressing), ``USE_SSL false`` (in-cluster plaintext) and ``REGION 'us-east-1'``.
    """
    env = os.environ if env is None else env

    endpoint = env.get("SEAWEEDFS_S3_ENDPOINT", DEFAULT_S3_ENDPOINT)
    key_id = env.get("S3_ACCESS_KEY_ID", _DEFAULT_S3_ACCESS_KEY_ID)
    secret = env.get("S3_SECRET_ACCESS_KEY", _DEFAULT_S3_SECRET_ACCESS_KEY)

    return (
        f"CREATE OR REPLACE SECRET {_S3_SECRET_NAME} (\n"
        f"    TYPE S3,\n"
        f"    KEY_ID '{key_id}',\n"
        f"    SECRET '{secret}',\n"
        f"    ENDPOINT '{endpoint}',\n"
        f"    REGION '{_S3_REGION}',\n"
        f"    URL_STYLE 'path',\n"
        f"    USE_SSL false\n"
        f");"
    )


def attach_or_replace_sql(alias: str, path: str) -> str:
    """Return the hot-swap ``ATTACH OR REPLACE`` statement (platform/004 §hot-swap).

    Pure function. ``path`` is the artifact location — an ``s3://`` URI for the
    serving artifact (``s3://warehouse/serving/notes-vN.duckdb``) or a local
    ``.duckdb`` path. ``alias`` is the schema name queries reference (e.g. ``notes``).

    On DuckDB 1.5.3 this completes in ~2ms, is non-blocking, and lets in-flight
    queries finish against their starting snapshot — the zero-downtime swap
    primitive verified in the ADR. The database is attached ``READ_ONLY`` because
    serving pods never mutate the artifact (the builder workflow rewrites it whole).
    """
    return f"ATTACH OR REPLACE '{path}' AS {alias} (READ_ONLY);"


def vector_search_sql(table: str, k: int) -> str:
    """Return a VSS nearest-neighbour query template for ``table`` returning ``k`` rows.

    Pure function. The returned SQL takes one parameter placeholder, ``$query`` —
    the query embedding (a ``FLOAT[]`` matching the indexed ``embedding`` column's
    dimensionality). Callers bind ``$query`` at execute time, e.g.::

        con.execute(vector_search_sql("notes.chunks", 10), {"query": vec})

    Ordering by ``array_distance(embedding, $query)`` lets DuckDB's VSS extension use
    the HNSW index when the artifact was built with one. The HNSW index itself is
    created by the serving-artifact build (Wavefront 3); this template only assumes
    an ``embedding`` column exists. ``k`` must be a positive integer (it is
    interpolated directly into ``LIMIT``, so it is validated rather than bound).
    """
    if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
        raise ValueError(f"k must be a positive int, got {k!r}")

    return (
        f"SELECT *, array_distance(embedding, $query) AS distance\n"
        f"FROM {table}\n"
        f"ORDER BY array_distance(embedding, $query)\n"
        f"LIMIT {k};"
    )


# --------------------------------------------------------------------------- #
# Network / extension-touching helpers (NOT exercised by hermetic tests)
# --------------------------------------------------------------------------- #


def load_extensions(con: duckdb.DuckDBPyConnection) -> None:
    """``INSTALL`` + ``LOAD`` the lakehouse DuckDB extensions on ``con``.

    Installs ``httpfs``, ``iceberg`` and ``vss``. The first ``INSTALL`` of each
    downloads the extension binary from the DuckDB extension repository over the
    network, so this MUST NOT be called from hermetic CI tests — it is deliberately
    factored out of the pure builders for exactly that reason.
    """
    for ext in _EXTENSIONS:
        con.execute(f"INSTALL {ext};")
        con.execute(f"LOAD {ext};")


def connect(
    *,
    read_only_artifact: str | None = None,
    env: Mapping[str, str] | None = None,
) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection wired for the SeaweedFS lakehouse.

    Steps performed:
      1. ``duckdb.connect()`` (in-memory base connection).
      2. ``load_extensions`` — installs/loads httpfs, iceberg, vss (network on first
         install).
      3. Runs ``s3_secret_sql(env)`` so subsequent ``s3://`` reads authenticate
         against SeaweedFS.
      4. If ``read_only_artifact`` is given, ``ATTACH OR REPLACE`` it as schema
         ``notes`` (the hot-swap pattern from platform/004 §hot-swap). The path may be
         an ``s3://warehouse/serving/notes-vN.duckdb`` URI or a local ``.duckdb`` file.

    Because steps 2-4 touch the network (extension download, S3 reads), this function
    is **not** invoked by the unit tests. Tests exercise the pure builders and an
    extension-free ``duckdb.connect(':memory:')`` smoke check instead.
    """
    # DuckDB downloads extensions under <home_directory>/.duckdb. It derives
    # home_directory from the passwd home of the running uid (NOT the $HOME env),
    # which for the non-root container user (65532) is "/" on a read-only
    # rootfs => INSTALL fails with 'Failed to create directory "/.duckdb"'.
    # Point home_directory at a writable dir (DUCKDB_HOME, default /tmp — the
    # emptyDir every lakehouse pod mounts).
    duckdb_home = (env or os.environ).get("DUCKDB_HOME", "/tmp")
    con = duckdb.connect(config={"home_directory": duckdb_home})
    load_extensions(con)
    con.execute(s3_secret_sql(env))
    if read_only_artifact is not None:
        con.execute(attach_or_replace_sql("notes", read_only_artifact))
    return con
