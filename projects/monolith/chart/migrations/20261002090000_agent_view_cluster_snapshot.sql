-- Agent-readable summaries live separately from private application schemas.
-- agents_writer must never receive USAGE on home or its dashboard tables.
CREATE SCHEMA IF NOT EXISTS agent_view;

CREATE TABLE agent_view.cluster_snapshot (
    id          SMALLINT PRIMARY KEY DEFAULT 1,
    payload     JSONB NOT NULL,
    snapshot_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT cluster_snapshot_singleton CHECK (id = 1)
);

GRANT USAGE ON SCHEMA agent_view TO agents_writer;
GRANT SELECT ON agent_view.cluster_snapshot TO agents_writer;
