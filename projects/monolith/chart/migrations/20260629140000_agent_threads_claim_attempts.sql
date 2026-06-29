-- Bounded retry for transient microVM launch failures (fc-agentd, Option B).
--
-- Before this, fc-agentd's reconcile loop marked a PENDING thread FAILED on the
-- FIRST Claim() error, and FAILED is terminal. That is correct for a permanent
-- failure (bad arch, missing base snapshot) but wrong for a TRANSIENT one: during
-- an fc-agentd rollout the freshly-started daemon can briefly fail to launch a
-- guest (KVM / devmapper / firmware not warm yet), so any thread submitted in
-- that window was burned terminal with no retry. claim_attempts counts launch
-- failures so the loop can retry a bounded number of times (paced by the reconcile
-- poll, which is the backoff) before giving up and marking FAILED.
--
-- The retry path increments ONLY this column and leaves state = 'PENDING'
-- untouched, so it does not trip the agent_threads_pending_notify trigger (which
-- fires on UPDATE OF state) and therefore does not hot-loop off its own write; the
-- next poll tick re-attempts. A successful claim resets the counter to 0.
ALTER TABLE claude_agent.agent_threads
    ADD COLUMN claim_attempts INTEGER NOT NULL DEFAULT 0;
