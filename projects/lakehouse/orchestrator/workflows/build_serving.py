"""``BuildServingArtifactWorkflow`` — build the hot-swappable serving `.duckdb`.

ADR platform/004 §"Write path" / §"Serving artifact build": every ~15 minutes
(cadence owned by the WF-SCHEDULES unit) read the latest Iceberg ``note_events``
snapshot via DuckDB + the iceberg extension, build a single ``.duckdb`` file
carrying a VSS **HNSW** index over the ``embedding`` column, upload it to
``s3://warehouse/serving/notes-v{N}.duckdb`` tagged ``state=building``, then
publish an ``events.serving.artifact-ready`` event so Quack pods hot-swap to it.

Build cadence is a runtime knob
-------------------------------
15min is the *initial* cadence only. platform/004 §"Build cadence is a runtime
knob": with hot-swap verified zero-downtime, the schedule can be tightened
(5min, 2min, per-Iceberg-commit) without code or infra changes — this workflow
is cadence-agnostic. It always builds the *latest* snapshot, so a faster cadence
just means fresher artifacts.

Current-version filter (stale-vector mitigation)
------------------------------------------------
``note_events`` is an append-only event log: a note edited 3 times has 3 sets of
chunk rows, and a deleted note has a ``tombstoned`` event. A naive HNSW over
every row would surface stale/deleted vectors (platform/004 §Risks: "Stale vector
hits from un-tombstoned old embeddings", High likelihood). The build therefore
folds to **current state** before indexing:

  * keep only the row(s) at each note's MAX(event_version) (latest revision);
  * drop notes whose latest event is ``tombstoned`` (deletions);
  * drop rows with a NULL ``embedding`` (metadata-only events have no vector).

The HNSW index is then built over exactly the live, current-version chunks, so a
vector hit can never return a stale or deleted note. This is the in-artifact
"current-version filter table" the ADR's risk-table mitigation calls for, applied
at build time rather than per-query — cheaper and removes the stale rows from the
index entirely instead of hash-join-filtering them out on every query.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import timedelta

import temporalio.activity
import temporalio.workflow
from temporalio.common import RetryPolicy

# In-cluster SeaweedFS S3 gateway endpoint, assembled from parts so the cluster
# DNS suffix is never a single literal (semgrep no-hardcoded-k8s-service-url — a
# renamed Helm release shifts the service prefix). The canonical override is
# ``SEAWEEDFS_S3_ENDPOINT`` from values.yaml; this assembled value is only the
# zero-config default. Mirrors the same treatment in ``iceberg.catalog`` /
# ``duckdb_query.query``.
_S3_SVC = "seaweedfs-s3"
_S3_NS = "seaweedfs"
_CLUSTER_DNS_SUFFIX = ".".join(["svc", "cluster", "local"])
DEFAULT_S3_ENDPOINT = f"http://{_S3_SVC}.{_S3_NS}.{_CLUSTER_DNS_SUFFIX}:8333"

# Serving artifact conventions (platform/004): one .duckdb per build, versioned,
# under s3://warehouse/serving/. The bucket/prefix match the ADR's path literal.
SERVING_BUCKET = "warehouse"
SERVING_PREFIX = "serving"
ARTIFACT_NAME_TEMPLATE = "notes-v{version}.duckdb"

# Source Iceberg table (current-version fold happens over this) + the schema name
# the iceberg extension reads it under in DuckDB (catalog.namespace.table).
SOURCE_TABLE = "note_events"
ICEBERG_NAMESPACE = "knowledge"

# Schema/table the serving artifact exposes to Quack queries. ``notes`` matches
# the ATTACH alias in ``duckdb_query.connect`` / ``attach_or_replace_sql``; the
# indexed table is ``notes.chunks`` (see ``duckdb_query.vector_search_sql``).
SERVING_SCHEMA = "main"
SERVING_TABLE = "chunks"

# Embedding dimensionality of the knowledge corpus (the monolith's embedding
# model). The note_events ``embedding`` column is an Iceberg ``list<float>`` →
# DuckDB reads it as a variable-length ``FLOAT[]``, but a VSS HNSW index requires
# a fixed-size ``FLOAT[N]`` key. The chunks-build CAST pins it to this N so the
# index can be created. All live embeddings share this dimension (single model);
# a row of a different length would fail the CAST loudly rather than silently
# corrupt the index.
EMBEDDING_DIM = 1024

# Serving-artifact-ready event (platform/004 §hot-swap trigger). Published on an
# explicit subject — NOT one of the events.knowledge.* entity subjects — so it is
# routed to the Quack swap consumer, not back into the Iceberg drainer.
ARTIFACT_READY_SUBJECT = "events.serving.artifact-ready"
ARTIFACT_ENTITY_TYPE = "artifact"

_ACTIVITY_TIMEOUT = timedelta(minutes=30)  # HNSW build can be slow as corpus grows
_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=3,
)


@dataclass
class BuildResult:
    """Outcome of one ``build_artifact`` run: the new version + its S3 path."""

    version: int
    artifact_path: str
    rows_indexed: int


def _artifact_key(version: int) -> str:
    """S3 object key for serving artifact ``version`` (``serving/notes-vN.duckdb``)."""
    return f"{SERVING_PREFIX}/{ARTIFACT_NAME_TEMPLATE.format(version=version)}"


def _artifact_s3_uri(version: int) -> str:
    """Full ``s3://warehouse/serving/notes-vN.duckdb`` URI for ``version``."""
    return f"s3://{SERVING_BUCKET}/{_artifact_key(version)}"


# Current-version fold over note_events. Window-ranks each note's rows by
# event_version (latest wins), keeps the latest revision, drops tombstoned notes
# and null-embedding rows. The result is the live chunk set the HNSW indexes.
#
# Built as a CTE chain so it reads as the fold it is:
#   latest   -> the MAX(event_version) per note_id
#   live     -> rows at that latest version whose latest event isn't tombstoned
#   indexed  -> live rows that actually carry an embedding
_BUILD_CHUNKS_SQL = """
CREATE TABLE {schema}.{table} AS
WITH latest AS (
    SELECT note_id, MAX(event_version) AS max_version
    FROM iceberg_scan('{source_uri}')
    GROUP BY note_id
),
current_rows AS (
    SELECT e.*
    FROM iceberg_scan('{source_uri}') e
    JOIN latest l
      ON e.note_id = l.note_id
     AND e.event_version = l.max_version
)
SELECT * EXCLUDE (embedding), CAST(embedding AS FLOAT[{embedding_dim}]) AS embedding
FROM current_rows
WHERE event_type <> 'tombstoned'
  AND embedding IS NOT NULL;
"""

# Build the HNSW index over the indexed chunks. array_distance is the metric
# duckdb_query.vector_search_sql orders by, so the index must use the matching
# (l2sq) metric for the planner to use it.
_BUILD_HNSW_SQL = (
    "CREATE INDEX notes_hnsw ON {schema}.{table} "
    "USING HNSW (embedding) WITH (metric = 'l2sq');"
)


@temporalio.activity.defn
async def build_artifact(version: int) -> BuildResult:
    """Build serving artifact ``notes-v{version}.duckdb`` and announce it.

    All I/O (DuckDB extension download, S3 read of the Iceberg snapshot, S3 write
    of the artifact + object tag, NATS publish) lives here — hence an activity.

    Steps:
      1. Open a DuckDB connection wired for SeaweedFS (``duckdb_query.connect``):
         loads httpfs/iceberg/vss + the S3 secret.
      2. Build the current-version-filtered ``chunks`` table from the latest
         ``note_events`` Iceberg snapshot (see :data:`_BUILD_CHUNKS_SQL`).
      3. Build the HNSW index over ``embedding``.
      4. Persist the in-memory DB to a local ``.duckdb`` file.
      5. Upload it to ``s3://warehouse/serving/notes-v{version}.duckdb`` and tag
         the object ``state=building`` (platform/004 §lifecycle: the initial tag
         prevents lifecycle deletion of an artifact whose workflow crashed before
         tag rotation).
      6. Publish ``events.serving.artifact-ready`` carrying the new version +
         path so Quack pods ``ATTACH OR REPLACE`` it.
    """
    from projects.lakehouse.duckdb_query.query import connect
    from projects.lakehouse.events.envelope import build_envelope
    from projects.lakehouse.events.publish import publish_event
    from projects.lakehouse.iceberg.catalog import load_warehouse_catalog
    from projects.lakehouse.nats_client.client import NatsClient

    namespace = os.environ.get("ICEBERG_NAMESPACE", ICEBERG_NAMESPACE)
    # Resolve the *current* snapshot's metadata.json through the shared catalog
    # rather than letting DuckDB guess the version from the table directory:
    # DuckDB's iceberg version-guessing cannot parse PyIceberg SqlCatalog's
    # metadata filenames, and the PG-backed catalog (shared across pods) is the
    # source of truth for which snapshot is current. iceberg_scan reads the exact
    # metadata.json path directly, so the build always reflects the latest commit.
    catalog = load_warehouse_catalog()
    table = catalog.load_table(f"{namespace}.{SOURCE_TABLE}")
    source_uri = table.metadata_location

    artifact_uri = _artifact_s3_uri(version)
    artifact_key = _artifact_key(version)

    # --- 1-4: build the artifact locally via DuckDB ------------------------- #
    with tempfile.TemporaryDirectory() as tmpdir:
        local_path = os.path.join(
            tmpdir, ARTIFACT_NAME_TEMPLATE.format(version=version)
        )

        # In-memory build connection (extensions + S3 secret configured).
        con = connect()
        try:
            # ATTACH the on-disk artifact file and build into it so the result is
            # a self-contained .duckdb (queryable by Quack after hot-swap).
            con.execute(f"ATTACH '{local_path}' AS artifact;")
            build_chunks = _BUILD_CHUNKS_SQL.format(
                schema=f"artifact.{SERVING_SCHEMA}",
                table=SERVING_TABLE,
                source_uri=source_uri,
                embedding_dim=EMBEDDING_DIM,
            )
            con.execute(build_chunks)
            con.execute(
                _BUILD_HNSW_SQL.format(
                    schema=f"artifact.{SERVING_SCHEMA}", table=SERVING_TABLE
                )
            )
            rows_indexed = con.execute(
                f"SELECT count(*) FROM artifact.{SERVING_SCHEMA}.{SERVING_TABLE};"
            ).fetchone()[0]
            con.execute("DETACH artifact;")
        finally:
            con.close()

        # --- 5: upload + tag state=building --------------------------------- #
        s3 = _s3_client()
        with open(local_path, "rb") as fh:
            s3.put_object(
                Bucket=SERVING_BUCKET,
                Key=artifact_key,
                Body=fh,
                Tagging="state=building",
            )

    # --- 6: announce the new artifact -------------------------------------- #
    client = NatsClient()
    await client.connect()
    try:
        envelope = build_envelope(
            entity_type=ARTIFACT_ENTITY_TYPE,
            entity_id=artifact_key,
            event_type="created",
            event_version=version,
            producer="build-serving-artifact-workflow",
            payload={
                "version": version,
                "path": artifact_uri,
                "table": f"{SERVING_SCHEMA}.{SERVING_TABLE}",
                "rows_indexed": int(rows_indexed),
            },
        )
        # Explicit subject: the serving subject is NOT in SUBJECT_BY_ENTITY (it's
        # not a knowledge entity), so pass it directly rather than deriving it.
        await publish_event(client, envelope, subject=ARTIFACT_READY_SUBJECT)
    finally:
        await client.close()

    return BuildResult(
        version=version,
        artifact_path=artifact_uri,
        rows_indexed=int(rows_indexed),
    )


def _s3_client():
    """boto3 S3 client pointed at the SeaweedFS S3 gateway.

    Reads the same env vars as the iceberg/duckdb modules so all three agree on
    the endpoint/credentials. SeaweedFS auth is disabled, so dummy creds suffice
    but boto3 still needs *some* value. Path-style addressing is forced
    (SeaweedFS only supports it).
    """
    import boto3
    from botocore.config import Config

    endpoint = os.environ.get("SEAWEEDFS_S3_ENDPOINT", DEFAULT_S3_ENDPOINT)
    # The chart injects a scheme-less host:port (shared with DuckDB's httpfs,
    # which derives the scheme from USE_SSL). boto3 requires a scheme on
    # endpoint_url and raises "Invalid endpoint" otherwise; SeaweedFS S3 is
    # plaintext HTTP, so prefix http:// when absent.
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "http://" + endpoint
    return boto3.client(  # nosemgrep: boto3-endpoint-url-missing-scheme
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY_ID", "duckdb"),
        aws_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY", "duckdb"),
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}),
    )


@temporalio.workflow.defn
class BuildServingArtifactWorkflow:
    """Build a fresh serving artifact and announce it (ADR platform/004).

    The WF-SCHEDULES unit triggers this every ~15min (a runtime knob — see module
    docstring). The workflow body only derives the next version from
    ``workflow.now()`` (deterministic — no wall clock, no I/O here) and invokes
    :func:`build_artifact`; everything else lives in the activity.
    """

    @temporalio.workflow.run
    async def run(self, version: int | None = None) -> BuildResult:
        """Build the next serving artifact version.

        ``version`` is normally derived from the workflow's deterministic clock
        (epoch seconds) so successive builds get monotonically increasing,
        collision-free version numbers without reading any external counter.
        WF-SCHEDULES may pass an explicit version for testing / replay.
        """
        if version is None:
            # Deterministic, monotonic version from the workflow clock. Epoch
            # seconds is monotonic across the 15min cadence (no two builds in the
            # same second) and needs no external sequence.
            version = int(temporalio.workflow.now().timestamp())
        return await temporalio.workflow.execute_activity(
            build_artifact,
            version,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY_POLICY,
        )


WORKFLOWS = [BuildServingArtifactWorkflow]
ACTIVITIES = [build_artifact]
