-- NULL retains the legacy receipt protocol during mixed-version rollout.
-- Prepared rows remain superseded/excluded until their exact owner records
-- attempt intent. No existing receipt, hold, claim or permit is changed.
ALTER TABLE agent_sessions.result_receipts
    ADD COLUMN IF NOT EXISTS attempt_phase TEXT;
