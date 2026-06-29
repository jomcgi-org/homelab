-- ADR 026 Phase 2: stateful artifact iteration. A goosecracker reply can resume
-- the thread's persisted goose session (Model A: restore the SQLite session +
-- prior artifact into a fresh fast-cold VM and `goose run --resume`) instead of
-- cold-rebuilding the whole artifact from the full transcript (Model B). The
-- `resume` flag on the thread row is how dispatch.submit signals that mode to
-- fc-agentd, which injects GOOSE_RESUME=1 into the guest. Defaults false so every
-- existing/cold path is unchanged.

ALTER TABLE claude_agent.agent_threads
    ADD COLUMN resume BOOLEAN NOT NULL DEFAULT false;
