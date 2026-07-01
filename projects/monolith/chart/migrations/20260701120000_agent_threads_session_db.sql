-- ADR 026 Phase 2: persist the thread's goose SQLite session so a reply can
-- resume the prior conversation (Model A) instead of cold-rebuilding from the
-- full transcript (Model B). The goosecracker runner exports the guest's
-- sessions.db after a successful run and restores it before the next; this column
-- is the durable store for that blob, keyed by the run ledger's session_id.
--
-- BYTEA (not S3) for now: SeaweedFS S3 auth is currently disabled, and the blob is
-- kilobytes (goose exits between turns, so the file is consistent at export). The
-- goosecracker.sessions module is the single seam, so moving this to an S3 object
-- store with presigned URLs later (guest still holds no credential) is a localized
-- change with no schema churn beyond dropping this column.
--
-- Nullable: a first (cold) run has no prior session, and legacy rows predate the
-- column; the runner treats an absent blob as "cold run, no resume".

ALTER TABLE claude_agent.agent_threads
    ADD COLUMN session_db BYTEA;
