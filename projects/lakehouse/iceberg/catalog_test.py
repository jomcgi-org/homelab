"""Tests for catalog_config — pure dict construction from env (no I/O)."""

from __future__ import annotations

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
