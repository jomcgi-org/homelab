-- ADR 026 Phase 2: move the goose session store from Postgres to S3. The
-- session_db BYTEA column (added 20260701120000) was an interim store taken while
-- SeaweedFS S3 was assumed unavailable; S3 is in fact the intended store (the
-- session sits at s3://artifacts/<id>/sessions.db alongside the artifact HTML,
-- ADR 024 + ADR 026), and goosecracker.sessions now reads/writes it via
-- artifact.s3. Drop the now-unused column. Safe: the blob is a resumability
-- optimization, so any in-flight session simply cold-rebuilds on its next reply.

ALTER TABLE claude_agent.agent_threads
    DROP COLUMN IF EXISTS session_db;
