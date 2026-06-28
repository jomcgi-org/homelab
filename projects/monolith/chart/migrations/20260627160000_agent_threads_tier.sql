-- Adds the per-thread model tier to claude_agent.agent_threads (ADR 024). The
-- tier is exactly the set of harness env the controller injects into the guest:
-- the model endpoint plus the secret PLACEHOLDERS the guest is allowed to hold.
-- "artifact" reaches Gemini via OpenRouter (the OpenRouter key swapped at the
-- egress hop, ADR 023 6b) and holds no other credential; the default/empty tier
-- reaches in-cluster Qwen.
--
-- Nullable: legacy rows predate the column, and a resume (wake) does not
-- re-supply it, so the controller treats an absent tier as the default tier
-- (in-cluster Qwen) rather than a new assignment.

ALTER TABLE claude_agent.agent_threads
    ADD COLUMN tier TEXT;
