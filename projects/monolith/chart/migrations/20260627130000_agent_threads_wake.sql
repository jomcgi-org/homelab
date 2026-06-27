-- Adds the wake-request seam to claude_agent.agent_threads (ADR 022, Phase 3).
--
-- The controller is a Postgres-reconcile loop: desired state in the table, the
-- daemon drives Firecracker to match it. `state` is the actual lifecycle state
-- the controller owns; `wake_requested_at` is the one piece of *desired* state a
-- caller sets to ask an IDLE thread to be restored (from the catalog's
-- resume-agent-thread, a Discord reply, or a CI event). The reconcile loop
-- restores threads whose wake_requested_at is set, then clears it.
--
-- last_active_at is bumped on a wake request so the GC's idle-TTL sweep does not
-- reclaim a thread that was just asked to resume.

ALTER TABLE claude_agent.agent_threads
    ADD COLUMN wake_requested_at TIMESTAMPTZ;

-- The reconcile loop scans for IDLE threads with a pending wake request.
CREATE INDEX idx_agent_threads_wake
    ON claude_agent.agent_threads (wake_requested_at)
    WHERE wake_requested_at IS NOT NULL;
