-- Heartbeat leader election for the monolith's in-process singletons (Discord
-- bot, AIS ingest, scheduler loop). A single lease row is renewed by the leader
-- every couple of seconds; any replica may steal it once heartbeat_at is older
-- than the TTL (missed heartbeats). This lets the web tier scale horizontally
-- without running N duplicate bots/streams. One upsert per replica per interval,
-- so the load on Postgres is negligible.

CREATE TABLE scheduler.leader_lease (
    lease_key    TEXT PRIMARY KEY,
    holder       TEXT NOT NULL,
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
