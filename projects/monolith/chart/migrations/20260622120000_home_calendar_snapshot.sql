-- home.calendar_snapshot: single-row snapshot of the day's parsed calendar
-- events. Mirrors the observability snapshot pattern (20260617010000).
--
-- Previously home.calendar_poll warmed a process-local in-memory cache
-- (_cached_events) that only the polling replica saw. Persisting the parsed
-- events here makes the poll a stateless batch job (so it can run as an Argo
-- CronWorkflow off the API pod) and lets every API replica serve the same
-- events from one shared row instead of each warming its own cache - important
-- for horizontal scaling.
--
-- event_date is stored so the read path can return events only when the
-- snapshot is for the current day (a stale snapshot from a missed run shows
-- nothing rather than yesterday's events). Single row (id = 1), whole-day
-- events as JSONB (the read path returns them verbatim).

CREATE SCHEMA IF NOT EXISTS home;

CREATE TABLE home.calendar_snapshot (
    id          SMALLINT PRIMARY KEY DEFAULT 1,
    event_date  DATE NOT NULL,
    events      JSONB NOT NULL,
    snapshot_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT calendar_snapshot_singleton CHECK (id = 1)
);
