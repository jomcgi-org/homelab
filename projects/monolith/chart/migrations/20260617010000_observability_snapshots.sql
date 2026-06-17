-- observability schema: precomputed snapshots of the public main-page topology and
-- stats payloads (ADR 004 security/004 Layer 4). A scheduled rollup on the private
-- monolith (which has ClickHouse + Kubernetes access) builds the full payload and
-- upserts it here; both the private read path and the Phase 5 public service read
-- these rows instead of querying ClickHouse or the K8s API at request time. This is
-- what lets the public service drop CLICKHOUSE_* and K8s credentials entirely: its
-- whole dependency set becomes the Postgres replica.
--
-- Each table holds a single row (id = 1). Whole-payload JSONB rather than normalised
-- per-metric columns, because the payload interleaves ClickHouse scalars, sparklines,
-- a free-form metrics list, and K8s-derived cluster stats: the public service cannot
-- reassemble that, so it reads the assembled payload verbatim.

CREATE SCHEMA IF NOT EXISTS observability;

CREATE TABLE observability.topology_snapshot (
    id          SMALLINT PRIMARY KEY DEFAULT 1,
    payload     JSONB NOT NULL,
    snapshot_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT topology_snapshot_singleton CHECK (id = 1)
);

CREATE TABLE observability.stats_snapshot (
    id          SMALLINT PRIMARY KEY DEFAULT 1,
    payload     JSONB NOT NULL,
    snapshot_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT stats_snapshot_singleton CHECK (id = 1)
);

-- Read access for the Phase 5 public service (ADR 004). public_reader is created by
-- CNPG (spec.managed.roles); see 20260617000000_public_reader_role.sql.
GRANT USAGE ON SCHEMA observability TO public_reader;
GRANT SELECT ON observability.topology_snapshot TO public_reader;
GRANT SELECT ON observability.stats_snapshot TO public_reader;
