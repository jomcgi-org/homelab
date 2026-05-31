"""Tests for catalog_config — pure dict construction from env (no I/O)."""

from __future__ import annotations

import pytest

from projects.lakehouse.iceberg.catalog import (
    DEFAULT_S3_ENDPOINT,
    DEFAULT_WAREHOUSE,
    catalog_config,
)


def test_defaults_when_env_empty():
    """With an empty env, the in-cluster SeaweedFS defaults are used."""
    cfg = catalog_config(env={})
    assert cfg["type"] == "sql"
    assert cfg["s3.endpoint"] == DEFAULT_S3_ENDPOINT
    assert cfg["warehouse"] == DEFAULT_WAREHOUSE
    assert cfg["s3.region"] == "us-east-1"
    # Dummy creds default to empty (SeaweedFS auth disabled).
    assert cfg["s3.access-key-id"] == ""
    assert cfg["s3.secret-access-key"] == ""


def test_path_style_access_for_seaweedfs():
    """SeaweedFS needs path-style addressing: path-style-access true AND
    force-virtual-addressing false (the key pyiceberg's pyarrow FileIO reads)."""
    cfg = catalog_config(env={})
    assert cfg["s3.path-style-access"] == "true"
    assert cfg["s3.force-virtual-addressing"] == "false"


def test_env_overrides_are_honored():
    """Endpoint, warehouse, creds, region, and catalog URI come from env."""
    env = {
        "SEAWEEDFS_S3_ENDPOINT": "http://s3.example:9000",
        "ICEBERG_WAREHOUSE": "s3://other-bucket/",
        "ICEBERG_CATALOG_URI": "sqlite:////tmp/cat.db",
        "S3_ACCESS_KEY_ID": "AKIA-test",
        "S3_SECRET_ACCESS_KEY": "secret-test",
        "AWS_REGION": "eu-west-2",
    }
    cfg = catalog_config(env=env)
    assert cfg["s3.endpoint"] == "http://s3.example:9000"
    assert cfg["warehouse"] == "s3://other-bucket/"
    assert cfg["uri"] == "sqlite:////tmp/cat.db"
    assert cfg["s3.access-key-id"] == "AKIA-test"
    assert cfg["s3.secret-access-key"] == "secret-test"
    assert cfg["s3.region"] == "eu-west-2"


def test_config_is_all_strings():
    """load_catalog takes str properties; every value must be a str."""
    cfg = catalog_config(env={})
    for key, value in cfg.items():
        assert isinstance(value, str), f"{key} is not a str: {value!r}"


@pytest.mark.parametrize(
    "key",
    [
        "type",
        "uri",
        "warehouse",
        "s3.endpoint",
        "s3.region",
        "s3.path-style-access",
        "s3.force-virtual-addressing",
    ],
)
def test_required_keys_present(key):
    """Every key the catalog/FileIO relies on is always emitted."""
    assert key in catalog_config(env={})


def test_catalog_uri_derived_from_database_url():
    """With DATABASE_URL set (and no explicit override), the catalog URI reuses
    those credentials but points at the dedicated `lakehouse` database with the
    psycopg v3 driver — and preserves the query string (sslmode, etc.)."""
    cfg = catalog_config(
        env={
            "DATABASE_URL": "postgresql://app:pw@monolith-pg-rw.monolith:5432/app?sslmode=require",
        }
    )
    assert (
        cfg["uri"]
        == "postgresql+psycopg://app:pw@monolith-pg-rw.monolith:5432/lakehouse?sslmode=require"
    )


def test_explicit_catalog_uri_wins_over_database_url():
    """An explicit ICEBERG_CATALOG_URI overrides the DATABASE_URL derivation."""
    cfg = catalog_config(
        env={
            "ICEBERG_CATALOG_URI": "sqlite:////tmp/cat.db",
            "DATABASE_URL": "postgresql://app:pw@host:5432/app",
        }
    )
    assert cfg["uri"] == "sqlite:////tmp/cat.db"


def test_catalog_db_name_is_overridable():
    """ICEBERG_CATALOG_DB chooses the database when deriving from DATABASE_URL."""
    cfg = catalog_config(
        env={
            "DATABASE_URL": "postgresql://app:pw@host:5432/app",
            "ICEBERG_CATALOG_DB": "lakehouse_alt",
        }
    )
    assert cfg["uri"] == "postgresql+psycopg://app:pw@host:5432/lakehouse_alt"


def test_sqlite_fallback_when_no_database_url():
    """No override and no DATABASE_URL -> the SQLite test fallback (type stays sql)."""
    cfg = catalog_config(env={})
    assert cfg["type"] == "sql"
    assert cfg["uri"].startswith("sqlite://")


def test_scheme_less_endpoint_gets_http_prefix():
    """A scheme-less endpoint (the chart's shared host:port) is prefixed with
    http:// — pyiceberg hands it to pyarrow which would otherwise default to
    https and fail the TLS handshake against plaintext SeaweedFS."""
    cfg = catalog_config(
        env={"SEAWEEDFS_S3_ENDPOINT": "seaweedfs-s3.seaweedfs.svc.cluster.local:8333"}
    )
    assert cfg["s3.endpoint"] == "http://seaweedfs-s3.seaweedfs.svc.cluster.local:8333"


def test_explicit_scheme_endpoint_is_preserved():
    """An endpoint that already carries a scheme is left untouched (either scheme)."""
    assert (
        catalog_config(env={"SEAWEEDFS_S3_ENDPOINT": "https://s3.example:9000"})[
            "s3.endpoint"
        ]
        == "https://s3.example:9000"
    )
    assert (
        catalog_config(env={"SEAWEEDFS_S3_ENDPOINT": "http://s3.example:9000"})[
            "s3.endpoint"
        ]
        == "http://s3.example:9000"
    )
