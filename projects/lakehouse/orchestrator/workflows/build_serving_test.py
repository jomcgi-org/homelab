"""Hermetic tests for ``build_serving`` (no Temporal server, no network).

Exercises the ``build_artifact`` activity with a mocked DuckDB connection, mocked
boto3 S3 client, and mocked NatsClient — asserting the current-version-filter SQL
shape, the HNSW index build, the ``state=building`` tag on upload, and the
``events.serving.artifact-ready`` publish. Also asserts the workflow is a
``@workflow.defn`` in ``WORKFLOWS`` and the SQL/path conventions.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import temporalio.workflow

from projects.lakehouse.orchestrator.workflows import build_serving as mod


# --------------------------------------------------------------------------- #
# Definition / registration / pure path + SQL helpers
# --------------------------------------------------------------------------- #


def test_workflow_is_defn_and_exported() -> None:
    assert mod.BuildServingArtifactWorkflow in mod.WORKFLOWS
    defn = temporalio.workflow._Definition.from_class(mod.BuildServingArtifactWorkflow)
    assert defn is not None


def test_activity_exported() -> None:
    assert mod.build_artifact in mod.ACTIVITIES
    assert hasattr(mod.build_artifact, "__temporal_activity_definition")


def test_artifact_path_convention() -> None:
    # platform/004: s3://warehouse/serving/notes-vN.duckdb
    assert mod._artifact_key(7) == "serving/notes-v7.duckdb"
    assert mod._artifact_s3_uri(7) == "s3://warehouse/serving/notes-v7.duckdb"


def test_artifact_ready_subject_is_explicit_serving_subject() -> None:
    # Must NOT collide with the knowledge entity subjects (would loop into the
    # Iceberg drainer); it's an explicit serving subject.
    from projects.lakehouse.events.publish import SUBJECT_BY_ENTITY

    assert mod.ARTIFACT_READY_SUBJECT == "events.serving.artifact-ready"
    assert mod.ARTIFACT_READY_SUBJECT not in SUBJECT_BY_ENTITY.values()


def test_build_chunks_sql_applies_current_version_filter() -> None:
    sql = mod._BUILD_CHUNKS_SQL.format(
        schema="artifact.main",
        table="chunks",
        source_uri="s3://warehouse/knowledge/note_events",
    )
    # Latest-version fold: MAX(event_version) per note_id, joined back.
    assert "MAX(event_version)" in sql
    assert "GROUP BY note_id" in sql
    # Drops tombstoned (deleted) notes — stale-vector mitigation.
    assert "event_type <> 'tombstoned'" in sql
    # Drops metadata-only rows with no vector.
    assert "embedding IS NOT NULL" in sql
    # Reads the Iceberg snapshot, not a raw parquet path.
    assert "iceberg_scan('s3://warehouse/knowledge/note_events')" in sql


def test_build_hnsw_sql_indexes_embedding() -> None:
    sql = mod._BUILD_HNSW_SQL.format(schema="artifact.main", table="chunks")
    assert "USING HNSW (embedding)" in sql
    # Metric must match duckdb_query.vector_search_sql's array_distance (l2sq).
    assert "metric = 'l2sq'" in sql


# --------------------------------------------------------------------------- #
# build_artifact activity
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_build_artifact_builds_indexes_tags_and_publishes() -> None:
    con = MagicMock()
    # rows_indexed query result
    con.execute.return_value.fetchone.return_value = (42,)

    s3 = MagicMock()
    nats_client = AsyncMock()

    publish_calls: list = []

    async def fake_publish(client, envelope, *, subject=None):
        publish_calls.append((client, envelope, subject))

    # The mocked DuckDB connection never writes the artifact file, so stub out
    # open() for the upload step (we assert on put_object's kwargs, not bytes).
    fake_open = MagicMock()
    fake_open.return_value.__enter__.return_value = b""

    with (
        patch("projects.lakehouse.duckdb_query.query.connect", return_value=con),
        patch.object(mod, "_s3_client", return_value=s3),
        patch(
            "projects.lakehouse.nats_client.client.NatsClient",
            return_value=nats_client,
        ),
        patch(
            "projects.lakehouse.events.publish.publish_event",
            side_effect=fake_publish,
        ),
        patch("builtins.open", fake_open),
    ):
        result = await mod.build_artifact(123)

    assert result.version == 123
    assert result.artifact_path == "s3://warehouse/serving/notes-v123.duckdb"
    assert result.rows_indexed == 42

    # The DuckDB build ran the current-version-filter CREATE TABLE + HNSW index.
    executed = " ".join(str(c.args[0]) for c in con.execute.call_args_list)
    assert "MAX(event_version)" in executed
    assert "USING HNSW (embedding)" in executed
    assert "event_type <> 'tombstoned'" in executed

    # Uploaded with state=building tag (platform/004 lifecycle initial tag).
    s3.put_object.assert_called_once()
    _, put_kwargs = s3.put_object.call_args
    assert put_kwargs["Bucket"] == "warehouse"
    assert put_kwargs["Key"] == "serving/notes-v123.duckdb"
    assert put_kwargs["Tagging"] == "state=building"

    # Published artifact-ready on the explicit serving subject with version+path.
    assert len(publish_calls) == 1
    _client, envelope, subject = publish_calls[0]
    assert subject == "events.serving.artifact-ready"
    assert envelope.entity_type == "artifact"
    assert envelope.payload["version"] == 123
    assert envelope.payload["path"] == "s3://warehouse/serving/notes-v123.duckdb"

    nats_client.connect.assert_awaited_once()
    nats_client.close.assert_awaited_once()


# --------------------------------------------------------------------------- #
# _s3_client wiring (path-style, SeaweedFS endpoint)
# --------------------------------------------------------------------------- #


def test_s3_client_uses_path_style_and_env_endpoint() -> None:
    created = {}

    def fake_boto3_client(service, **kwargs):
        created["service"] = service
        created["kwargs"] = kwargs
        return MagicMock()

    fake_boto3 = MagicMock()
    fake_boto3.client.side_effect = fake_boto3_client

    with (
        patch.dict(
            "os.environ",
            {"SEAWEEDFS_S3_ENDPOINT": "http://sw:8333"},
            clear=False,
        ),
        patch.dict("sys.modules", {"boto3": fake_boto3}),
    ):
        mod._s3_client()

    assert created["service"] == "s3"
    assert created["kwargs"]["endpoint_url"] == "http://sw:8333"
    cfg = created["kwargs"]["config"]
    assert cfg.s3["addressing_style"] == "path"
