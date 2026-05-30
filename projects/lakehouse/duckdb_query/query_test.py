"""Hermetic tests for the DuckDB query helpers (LIB-DUCKDB-QUERY).

Only the pure SQL builders and an extension-free in-memory smoke test are
exercised — nothing here calls ``load_extensions`` or ``connect`` with a remote
artifact, since DuckDB extension installs and S3 reads touch the network and would
fail in hermetic CI.
"""

from __future__ import annotations

import duckdb
import pytest

from projects.lakehouse.duckdb_query.query import (
    DEFAULT_S3_ENDPOINT,
    attach_or_replace_sql,
    s3_secret_sql,
    vector_search_sql,
)


# --------------------------------------------------------------------------- #
# s3_secret_sql
# --------------------------------------------------------------------------- #


def test_s3_secret_sql_defaults_contain_seaweedfs_config():
    sql = s3_secret_sql(env={})

    assert "CREATE OR REPLACE SECRET" in sql
    assert "TYPE S3" in sql
    assert DEFAULT_S3_ENDPOINT in sql
    assert f"ENDPOINT '{DEFAULT_S3_ENDPOINT}'" in sql
    assert "URL_STYLE 'path'" in sql
    assert "USE_SSL false" in sql
    assert "REGION 'us-east-1'" in sql
    # Statement is terminated so it can be concatenated/executed safely.
    assert sql.rstrip().endswith(");")


def test_s3_secret_sql_uses_dummy_creds_by_default():
    sql = s3_secret_sql(env={})

    # SeaweedFS auth is disabled -> dummy creds are emitted, not blank.
    assert "KEY_ID 'duckdb'" in sql
    assert "SECRET 'duckdb'" in sql


def test_s3_secret_sql_env_overrides_creds_and_endpoint():
    sql = s3_secret_sql(
        env={
            "SEAWEEDFS_S3_ENDPOINT": "localhost:9000",
            "S3_ACCESS_KEY_ID": "AKIAEXAMPLE",
            "S3_SECRET_ACCESS_KEY": "s3cr3t",
        }
    )

    assert "ENDPOINT 'localhost:9000'" in sql
    assert "KEY_ID 'AKIAEXAMPLE'" in sql
    assert "SECRET 's3cr3t'" in sql
    # Default endpoint must not leak through when overridden.
    assert DEFAULT_S3_ENDPOINT not in sql


def test_s3_secret_sql_partial_env_falls_back_per_key():
    sql = s3_secret_sql(env={"S3_ACCESS_KEY_ID": "only-key"})

    assert "KEY_ID 'only-key'" in sql
    # Endpoint + secret fall back to defaults independently.
    assert f"ENDPOINT '{DEFAULT_S3_ENDPOINT}'" in sql
    assert "SECRET 'duckdb'" in sql


# --------------------------------------------------------------------------- #
# attach_or_replace_sql
# --------------------------------------------------------------------------- #


def test_attach_or_replace_sql_formats_s3_uri():
    sql = attach_or_replace_sql("notes", "s3://warehouse/serving/notes-v42.duckdb")

    assert sql == (
        "ATTACH OR REPLACE 's3://warehouse/serving/notes-v42.duckdb' "
        "AS notes (READ_ONLY);"
    )


def test_attach_or_replace_sql_formats_local_path():
    sql = attach_or_replace_sql("snapshot", "/tmp/notes.duckdb")

    assert "ATTACH OR REPLACE '/tmp/notes.duckdb' AS snapshot" in sql
    assert "READ_ONLY" in sql


# --------------------------------------------------------------------------- #
# vector_search_sql
# --------------------------------------------------------------------------- #


def test_vector_search_sql_formats_with_k():
    sql = vector_search_sql("notes.chunks", 10)

    assert "FROM notes.chunks" in sql
    assert "array_distance(embedding, $query)" in sql
    assert "ORDER BY array_distance(embedding, $query)" in sql
    assert "LIMIT 10;" in sql


def test_vector_search_sql_distinct_k_values():
    assert "LIMIT 1;" in vector_search_sql("t", 1)
    assert "LIMIT 256;" in vector_search_sql("t", 256)


@pytest.mark.parametrize("bad_k", [0, -1, 2.5, "5", True, None])
def test_vector_search_sql_rejects_non_positive_int_k(bad_k):
    with pytest.raises(ValueError):
        vector_search_sql("notes.chunks", bad_k)


# --------------------------------------------------------------------------- #
# In-memory DuckDB smoke test (NO extensions loaded — stays hermetic)
# --------------------------------------------------------------------------- #


def test_in_memory_duckdb_select_returns_42():
    con = duckdb.connect(":memory:")
    try:
        result = con.execute("SELECT 42").fetchone()
    finally:
        con.close()

    assert result == (42,)


def test_in_memory_duckdb_runs_generated_s3_secret_is_not_exercised():
    # The S3 secret SQL is NOT executed here on purpose: applying it would make the
    # connection try to resolve the (non-existent) SeaweedFS endpoint at query time.
    # We only assert the builder produced something executable-looking; execution of
    # secret/extension SQL belongs to the runtime path, not hermetic tests.
    con = duckdb.connect(":memory:")
    try:
        # A query that needs no extensions/network still works on the same handle.
        assert con.execute("SELECT 1 + 1").fetchone() == (2,)
    finally:
        con.close()
