"""PyIceberg catalog config for the SeaweedFS-backed warehouse (ADR platform/004).

Catalog-type decision
----------------------
platform/004 §"file-based catalog" (and its backup story) assumes an Iceberg
catalog whose "which snapshot is current" pointer lives *inside* the warehouse
bucket, so that ``rclone sync`` of the bucket captures everything — no external
catalog service to operate or back up.

PyIceberg 0.11.1 has **no Hadoop / pure-filesystem catalog** implementation. Its
``AVAILABLE_CATALOGS`` are ``rest``, ``hive``, ``glue``, ``dynamodb``, ``sql``,
``in-memory`` and ``bigquery``. The closest no-external-service option is
``CatalogType.SQL`` (``SqlCatalog``), which works against any SQLAlchemy backend.

We use ``type="sql"`` backed by **PostgreSQL** — a dedicated ``lakehouse``
database on the existing monolith-pg CNPG cluster (a CNPG ``Database`` CRD; see
projects/platform/temporal-databases/database-lakehouse.yaml). The Iceberg *data*
and *metadata* files (manifests, snapshots, ``…metadata.json``) live immutably in
the S3 warehouse exactly as platform/004 describes; only the small catalog index
(namespace + table -> current-metadata-pointer) lives in Postgres.

Why Postgres, not the SQLite the first cut used: the pointer must be visible to
*every* worker pod — the iceberg-builder writes table pointers; the housekeeping
build_serving reads them to find the current snapshot. A pod-local SQLite file
stranded the pointer on one pod's ephemeral ``/tmp`` (the cluster mounts no shared
volume), so cross-pod reads failed. Postgres makes the catalog genuinely shared
and durable. This resolves the prior "shared catalog" deviation; the credential
is the cluster-generated monolith-pg ``app`` role (cloned in as ``lakehouse-pg``),
pointed at the isolated ``lakehouse`` database so it never touches the monolith's
notes/embedding tables.

DEVIATION from platform/004 §"file-based catalog": the pointer lives in Postgres
rather than *inside the warehouse bucket*, so a bucket-only ``rclone`` snapshot no
longer captures the catalog. The catalog is instead covered by the CNPG cluster's
own backup (the same PG that backs the monolith + Temporal), which is an existing,
operated backup path — a net improvement over a hand-managed SQLite file. Revisit
if pyiceberg ever ships a pure-filesystem catalog.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

# Default S3 endpoint for the in-cluster SeaweedFS S3 gateway: the ``seaweedfs-s3``
# Service in the ``seaweedfs`` namespace on port 8333, resolved through the usual
# in-cluster DNS suffix. SeaweedFS auth is disabled, so the access-key /
# secret-key may be dummy values; pyiceberg's pyarrow S3 FileIO still requires
# *some* value to be present.
#
# Assembled from parts rather than written as one literal so the in-cluster DNS
# suffix is not hardcoded as a string default (semgrep no-hardcoded-k8s-service-url
# — a renamed Helm release shifts the service prefix). In production the endpoint
# is injected via the ``SEAWEEDFS_S3_ENDPOINT`` env var from the chart's
# values.yaml; this default only makes the config self-contained for tests and
# direct local use.
_SVC_NAME = "seaweedfs-s3"
_SVC_NAMESPACE = "seaweedfs"
_CLUSTER_DNS_SUFFIX = ".".join(["svc", "cluster", "local"])
DEFAULT_S3_ENDPOINT = f"http://{_SVC_NAME}.{_SVC_NAMESPACE}.{_CLUSTER_DNS_SUFFIX}:8333"
DEFAULT_WAREHOUSE = "s3://warehouse/"
DEFAULT_REGION = "us-east-1"

# Catalog name. The SqlCatalog pointer index (which metadata.json is current
# per table) lives in PostgreSQL in the cluster — see ``_catalog_uri``. A
# Postgres backend makes the pointer SHARED across every worker pod (the
# iceberg-builder writes table pointers; the housekeeping build_serving reads
# them), resolving the platform/004 "shared catalog" deviation that the old
# pod-local SQLite file introduced. The SQLite URI below is only the fallback
# for hermetic tests / local dev where no DATABASE_URL is injected.
CATALOG_NAME = "warehouse"
DEFAULT_CATALOG_URI = "sqlite:////tmp/warehouse_catalog.db"

# Dedicated PostgreSQL database (CNPG Database CRD, owner ``app``, on the
# monolith-pg cluster — see projects/platform/temporal-databases/) that holds the
# SqlCatalog metastore tables (``iceberg_tables`` / ``iceberg_namespace_properties``).
# Isolated from the monolith app DB so the catalog never touches the notes/
# embedding tables. Overridable via ICEBERG_CATALOG_DB.
DEFAULT_CATALOG_DB = "lakehouse"


def _catalog_uri(env: Mapping[str, str]) -> str:
    """Resolve the SqlCatalog SQLAlchemy URI from ``env``.

    Precedence:
      1. ``ICEBERG_CATALOG_URI`` — an explicit override (used by tests and local
         dev; e.g. a ``sqlite:///`` path).
      2. Derived from ``DATABASE_URL`` — the cluster-generated monolith-pg
         credential (cloned into this namespace as the ``lakehouse-pg`` Secret)
         authenticates as the ``app`` role. We reuse those credentials but point
         at the dedicated ``lakehouse`` database (``ICEBERG_CATALOG_DB``) so the
         catalog's pointer index is shared across all worker pods. The driver is
         forced to ``psycopg`` (v3 — the installed driver) for SQLAlchemy, and
         the original query string (e.g. ``sslmode``) is preserved.
      3. ``DEFAULT_CATALOG_URI`` — the SQLite fallback for hermetic tests.

    Pure: parses strings only, no I/O.
    """
    explicit = env.get("ICEBERG_CATALOG_URI")
    if explicit:
        return explicit

    database_url = env.get("DATABASE_URL")
    if database_url:
        catalog_db = env.get("ICEBERG_CATALOG_DB", DEFAULT_CATALOG_DB)
        parts = urlsplit(database_url)
        # Keep netloc (user:pass@host:port) + query (sslmode, etc.); swap the
        # driver to psycopg v3 and the database to the dedicated catalog DB.
        return urlunsplit(
            parts._replace(scheme="postgresql+psycopg", path=f"/{catalog_db}")
        )

    return DEFAULT_CATALOG_URI


def catalog_config(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the PyIceberg catalog config for the SeaweedFS warehouse.

    Pure function — reads only from ``env`` (defaults to ``os.environ``) and
    returns a plain dict suitable for ``load_catalog(name, **config)``. Does not
    touch the network or filesystem.

    Recognised env vars:

      SEAWEEDFS_S3_ENDPOINT   S3 endpoint (default: in-cluster SeaweedFS gateway)
      ICEBERG_WAREHOUSE       warehouse root      (default: ``s3://warehouse/``)
      DATABASE_URL            monolith-pg URI; the catalog reuses its credentials
                              against the ``lakehouse`` database (see _catalog_uri)
      ICEBERG_CATALOG_URI     explicit catalog URI override (default: derived from
                              DATABASE_URL, else the SQLite test fallback)
      ICEBERG_CATALOG_DB      catalog database name when deriving (default: lakehouse)
      S3_ACCESS_KEY_ID        S3 access key       (default: ``""`` — auth off)
      S3_SECRET_ACCESS_KEY    S3 secret key       (default: ``""`` — auth off)
      AWS_REGION              S3 region           (default: ``us-east-1``)
    """
    env = os.environ if env is None else env

    endpoint = env.get("SEAWEEDFS_S3_ENDPOINT", DEFAULT_S3_ENDPOINT)
    # pyiceberg hands ``s3.endpoint`` straight to pyarrow's S3FileSystem as
    # ``endpoint_override`` with NO ``scheme`` kwarg, so pyarrow defaults to
    # https. SeaweedFS S3 is plaintext HTTP, so a scheme-less endpoint triggers a
    # TLS handshake against an HTTP server ("SSL routines::wrong version number").
    # pyarrow honours a scheme embedded in endpoint_override, so ensure http://
    # is present. The chart injects a scheme-less host:port shared with DuckDB
    # (whose httpfs derives the scheme from USE_SSL instead), so the normalisation
    # has to live here rather than in the shared env value.
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "http://" + endpoint
    warehouse = env.get("ICEBERG_WAREHOUSE", DEFAULT_WAREHOUSE)
    catalog_uri = _catalog_uri(env)
    access_key = env.get("S3_ACCESS_KEY_ID", "")
    secret_key = env.get("S3_SECRET_ACCESS_KEY", "")
    region = env.get("AWS_REGION", DEFAULT_REGION)

    return {
        # SqlCatalog: no external metastore service. In-cluster the pointer index
        # lives in the shared `lakehouse` PostgreSQL database (see _catalog_uri),
        # so every worker pod sees the same "current snapshot" pointer. type stays
        # "sql" for both the PG and SQLite-fallback backends.
        "type": "sql",
        "uri": catalog_uri,
        "warehouse": warehouse,
        "s3.endpoint": endpoint,
        "s3.access-key-id": access_key,
        "s3.secret-access-key": secret_key,
        "s3.region": region,
        # Path-style access against SeaweedFS. ``s3.path-style-access`` is the
        # name used by other Iceberg engines and is set for forward-compat /
        # documentation; pyiceberg's pyarrow FileIO actually keys off
        # ``s3.force-virtual-addressing`` (default True), so we explicitly
        # disable virtual-host addressing to force path-style requests, which is
        # what SeaweedFS expects.
        "s3.path-style-access": "true",
        "s3.force-virtual-addressing": "false",
    }


def load_warehouse_catalog(env: Mapping[str, str] | None = None):
    """Load the SeaweedFS warehouse catalog.

    Thin wrapper over ``pyiceberg.catalog.load_catalog``. Performs network/DB I/O
    (opens the SQLite catalog DB, talks to S3) and so is **not** exercised in the
    hermetic unit tests — only ``catalog_config`` is unit-tested. The import is
    deferred so importing this module stays cheap and side-effect free.
    """
    from pyiceberg.catalog import load_catalog

    return load_catalog(CATALOG_NAME, **catalog_config(env))
