-- A single-row snapshot behind /api/agents/public/activity.
--
-- The endpoint used to aggregate public_api.agent_activity_daily on every
-- request. That view parses agent_turns.usage_json, which carries each turn's
-- tool-call activity list (28 KB on average, up to 1.2 MB), so one request
-- spent 4 to 6 seconds detoasting and casting JSON. The private-tier job
-- agent-activity-snapshot (app/jobs_main.py) now builds the list-price payload
-- on a cadence and upserts it here, the same pattern as
-- public_api.factory_activity_snapshot. The public route reads one row.
CREATE TABLE public_api.agent_activity_snapshot (
    id             SMALLINT PRIMARY KEY DEFAULT 1,
    payload        JSONB NOT NULL,
    snapshotted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT agent_activity_snapshot_singleton CHECK (id = 1)
);

GRANT SELECT ON public_api.agent_activity_snapshot TO public_reader;

-- The snapshot job and the factory board's 7-day spend both window turns by
-- created_at; without this every window is a sequential scan.
CREATE INDEX agent_turns_created_at_idx ON agent_sessions.agent_turns (created_at);
