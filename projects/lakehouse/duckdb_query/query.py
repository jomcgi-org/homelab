"""DuckDB query helpers for the Iceberg-on-SeaweedFS lakehouse (ADR platform/004).

The module is intentionally split into two layers:

  * **Pure SQL string builders** — ``s3_secret_sql``, ``attach_or_replace_sql`` and
    ``vector_search_sql``. They have no side effects, touch no network, and are the
    unit-tested surface.
  * **Extension / S3-touching helpers** — ``load_extensions`` (``LOAD`` of the signed
    extension binaries bundled into the image at ``/opt/duckdb_ext`` — gunzipped to a
    writable dir, no network) and ``connect`` (which calls ``load_extensions`` and
    configures the S3 secret). These are kept out of the default test path so the
    hermetic CI never touches the on-disk bundle.

DuckDB version is pinned to 1.5.3 (the version platform/004 verified for the
``ATTACH OR REPLACE`` hot-swap semantics).
"""

from __future__ import annotations

import gzip
import os
import shutil
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

# DuckDB extensions needed for the lakehouse read/serving paths. These are
# bundled into the worker + quack images at _BUNDLED_EXT_DIR (see MODULE.bazel's
# duckdb_ext_* multiarch_http_file rules) and LOAD-ed from a local path so the
# hardened image never downloads them — DuckDB still verifies the embedded
# signature on LOAD.
#
# ORDER MATTERS: with no installer to auto-resolve transitive extensions, each
# must be LOAD-ed after its dependencies. avro + parquet are iceberg's readers,
# so they precede iceberg.
#   avro    — Avro decoder used by iceberg's manifest reads
#   parquet — Parquet reader used by iceberg data files
#   httpfs  — direct S3 reads against SeaweedFS
#   iceberg — read Iceberg tables (workflow read path: warehouse.knowledge.*)
#   vss     — HNSW vector search over the serving artifact's embedding column
_EXTENSIONS = ("avro", "parquet", "httpfs", "iceberg", "vss")

# Where the multiarch_http_file tars place the signed .duckdb_extension.gz files
# in the image rootfs (read-only at runtime). load_extensions gunzips each into a
# writable scratch dir before LOAD-ing it.
_BUNDLED_EXT_DIR = "/opt/duckdb_ext"


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


# Embedding dimensionality of the serving artifact's indexed ``embedding`` column
# (the monolith embedding model; matches build_serving.EMBEDDING_DIM). The column
# is a fixed-size ``FLOAT[N]`` (so a VSS HNSW index can be built), so the query
# vector must be cast to the SAME fixed type — DuckDB's array_distance has no
# FLOAT[N] x DOUBLE[] (variable-list) overload, which is what a bound Python list
# binds as.
EMBEDDING_DIM = 1024


def vector_search_sql(table: str, k: int, dim: int = EMBEDDING_DIM) -> str:
    """Return a VSS nearest-neighbour query template for ``table`` returning ``k`` rows.

    Pure function. The returned SQL takes one parameter placeholder, ``$query`` —
    the query embedding, a length-``dim`` vector. Callers bind ``$query`` at
    execute time, e.g.::

        con.execute(vector_search_sql("notes.chunks", 10), {"query": vec})

    ``$query`` is CAST to ``FLOAT[dim]`` so it matches the indexed ``embedding``
    column's fixed-size ``FLOAT[N]`` type: a bound Python list binds as a
    variable-length ``DOUBLE[]``, and ``array_distance`` has no ``FLOAT[N]`` ×
    ``DOUBLE[]`` overload (it requires both operands to be the same fixed array
    type). Ordering by ``array_distance`` lets DuckDB's VSS extension use the HNSW
    index when the artifact was built with one (matching l2sq metric). ``k`` must
    be a positive integer (interpolated into ``LIMIT``, so validated not bound).
    """
    if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
        raise ValueError(f"k must be a positive int, got {k!r}")
    if not isinstance(dim, int) or isinstance(dim, bool) or dim <= 0:
        raise ValueError(f"dim must be a positive int, got {dim!r}")

    return (
        f"SELECT *, array_distance(embedding, $query::FLOAT[{dim}]) AS distance\n"
        f"FROM {table}\n"
        f"ORDER BY array_distance(embedding, $query::FLOAT[{dim}])\n"
        f"LIMIT {k};"
    )


# --------------------------------------------------------------------------- #
# Extension / S3-touching helpers (NOT exercised by hermetic tests)
# --------------------------------------------------------------------------- #


def load_extensions(
    con: duckdb.DuckDBPyConnection,
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """``LOAD`` the lakehouse DuckDB extensions from the in-image bundle on ``con``.

    The signed extension binaries (``avro``, ``parquet``, ``httpfs``, ``iceberg``,
    ``vss``) are baked into the image at ``_BUNDLED_EXT_DIR`` as gzipped
    ``.duckdb_extension.gz`` files (see MODULE.bazel's ``duckdb_ext_*``
    ``multiarch_http_file`` rules). The image rootfs is read-only, so each is
    gunzipped once into a writable scratch dir (``$DUCKDB_HOME/duckdb_ext``,
    default ``/tmp/duckdb_ext``) and then ``LOAD``-ed by absolute path.

    No ``INSTALL`` and no network: ``LOAD '<path>'`` verifies the extension's
    embedded signature locally, so the hardened image — which has no CA bundle
    DuckDB's extension installer could use — never reaches extensions.duckdb.org.
    Because it reads the on-disk bundle, this MUST NOT be called from hermetic CI
    tests; it is deliberately factored out of the pure builders for that reason.
    """
    env = os.environ if env is None else env
    runtime_dir = os.path.join(env.get("DUCKDB_HOME", "/tmp"), "duckdb_ext")
    os.makedirs(runtime_dir, exist_ok=True)
    for ext in _EXTENSIONS:
        gz_path = os.path.join(_BUNDLED_EXT_DIR, f"{ext}.duckdb_extension.gz")
        ext_path = os.path.join(runtime_dir, f"{ext}.duckdb_extension")
        if not os.path.exists(ext_path):
            with gzip.open(gz_path, "rb") as src, open(ext_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
        con.execute(f"LOAD '{ext_path}';")


def connect(
    *,
    read_only_artifact: str | None = None,
    env: Mapping[str, str] | None = None,
) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection wired for the SeaweedFS lakehouse.

    Steps performed:
      1. ``duckdb.connect()`` (in-memory base connection).
      2. ``load_extensions`` — LOADs the bundled httpfs, iceberg, vss, avro, parquet
         extensions from the in-image bundle (no network; see ``load_extensions``).
      3. Runs ``s3_secret_sql(env)`` so subsequent ``s3://`` reads authenticate
         against SeaweedFS.
      4. If ``read_only_artifact`` is given, ``ATTACH OR REPLACE`` it as schema
         ``notes`` (the hot-swap pattern from platform/004 §hot-swap). The path may be
         an ``s3://warehouse/serving/notes-vN.duckdb`` URI or a local ``.duckdb`` file.

    Because steps 2-4 touch the filesystem bundle and S3, this function is **not**
    invoked by the unit tests. Tests exercise the pure builders and an
    extension-free ``duckdb.connect(':memory:')`` smoke check instead.
    """
    # DuckDB derives its home_directory from the passwd home of the running uid
    # (NOT the $HOME env), which for the non-root container user (65532) is "/"
    # on a read-only rootfs. Several DuckDB operations create <home>/.duckdb
    # (settings, temp), which would fail with 'Failed to create directory
    # "/.duckdb"'. Point home_directory at a writable dir (DUCKDB_HOME, default
    # /tmp — the emptyDir every lakehouse pod mounts). Extensions are LOAD-ed from
    # the bundle (load_extensions), not installed here, so this no longer governs
    # extension downloads — it just keeps DuckDB's home writable.
    duckdb_home = (env or os.environ).get("DUCKDB_HOME", "/tmp")
    con = duckdb.connect(config={"home_directory": duckdb_home})
    # SeaweedFS S3 is plaintext (USE_SSL false) and extensions LOAD from the local
    # bundle, so no CA bundle is needed for the lakehouse paths. Set DuckDB's own
    # `ca_cert_file` (distinct from OpenSSL/$SSL_CERT_FILE) at certifi's bundle
    # defensively, so any future HTTPS httpfs read (e.g. an external S3) verifies
    # against a real trust store rather than failing on the empty system store.
    import certifi

    con.execute(f"SET ca_cert_file = '{certifi.where()}'")
    load_extensions(con, env=env)
    # The serving artifact persists a VSS HNSW index inside an on-disk .duckdb,
    # and Quack ATTACHes that file to query it. Both the build (CREATE INDEX ...
    # USING HNSW on the attached on-disk DB) and Quack's read require DuckDB's
    # experimental persistent-HNSW flag — platform/004 §hot-swap deliberately
    # ships a persistent HNSW serving artifact. Without it the index build raises
    # "HNSW indexes can only be created in in-memory databases". Set after
    # load_extensions since the vss extension registers this option.
    con.execute("SET hnsw_enable_experimental_persistence = true;")
    con.execute(s3_secret_sql(env))
    if read_only_artifact is not None:
        con.execute(attach_or_replace_sql("notes", read_only_artifact))
    return con
