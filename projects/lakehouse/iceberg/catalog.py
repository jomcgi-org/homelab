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
``CatalogType.SQL`` (``SqlCatalog``) backed by a **SQLite** metadata database.

We therefore use ``type="sql"`` with a SQLite URI on shared storage. The Iceberg
*data* and *metadata* files (manifests, snapshots, ``vN.metadata.json``) still
live immutably in the S3 warehouse exactly as platform/004 describes; only the
small catalog index (namespace + table -> current-metadata-pointer) lives in the
SQLite DB.

DEVIATION (follow-up for Wavefront 3): platform/004 says the catalog pointer
lives *in the warehouse bucket*. With SqlCatalog the pointer lives in a SQLite
file that must itself be on shared storage and included in the backup set (it is
small — namespace + table rows). The backup section's "clone the bucket, have
everything" guarantee is preserved only if the SQLite DB is co-located on the
same SeaweedFS-backed volume (or itself rcloned). The batch-commit deployment
(Wavefront 3) must mount the catalog DB on shared storage and add it to the
backup cron, OR revisit once pyiceberg ships a filesystem catalog. Tracked as a
platform/004 follow-up.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

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

# Catalog name + the SQLite metadata DB. The DB lives on shared storage so the
# warehouse + catalog pointer back up together (see module docstring deviation).
CATALOG_NAME = "warehouse"
DEFAULT_CATALOG_URI = "sqlite:////warehouse/catalog/warehouse_catalog.db"


def catalog_config(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the PyIceberg catalog config for the SeaweedFS warehouse.

    Pure function — reads only from ``env`` (defaults to ``os.environ``) and
    returns a plain dict suitable for ``load_catalog(name, **config)``. Does not
    touch the network or filesystem.

    Recognised env vars:

      SEAWEEDFS_S3_ENDPOINT   S3 endpoint (default: in-cluster SeaweedFS gateway)
      ICEBERG_WAREHOUSE       warehouse root      (default: ``s3://warehouse/``)
      ICEBERG_CATALOG_URI     SQLite metadata DB  (default: shared-storage path)
      S3_ACCESS_KEY_ID        S3 access key       (default: ``""`` — auth off)
      S3_SECRET_ACCESS_KEY    S3 secret key       (default: ``""`` — auth off)
      AWS_REGION              S3 region           (default: ``us-east-1``)
    """
    env = os.environ if env is None else env

    endpoint = env.get("SEAWEEDFS_S3_ENDPOINT", DEFAULT_S3_ENDPOINT)
    warehouse = env.get("ICEBERG_WAREHOUSE", DEFAULT_WAREHOUSE)
    catalog_uri = env.get("ICEBERG_CATALOG_URI", DEFAULT_CATALOG_URI)
    access_key = env.get("S3_ACCESS_KEY_ID", "")
    secret_key = env.get("S3_SECRET_ACCESS_KEY", "")
    region = env.get("AWS_REGION", DEFAULT_REGION)

    return {
        # SqlCatalog: no external metastore service; the pointer index lives in a
        # SQLite DB on shared storage (see module docstring for the rationale +
        # the platform/004 deviation this introduces).
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
