-- NULL retains the legacy receipt protocol during mixed-version rollout.
-- Prepared rows remain superseded/excluded until their exact owner records
-- attempt intent. No existing receipt, hold, claim or permit is changed.
ALTER TABLE agent_sessions.result_receipts
    ADD COLUMN IF NOT EXISTS attempt_phase TEXT;

-- An Applied migration must prove the shape even if ADD skipped an existing
-- column. Inspect metadata only; never repair a mismatch or rewrite receipts.
DO $receipt_attempt_phase$
BEGIN
    IF 1 <> (
        SELECT count(*)
        FROM pg_catalog.pg_attribute
        WHERE attrelid = 'agent_sessions.result_receipts'::pg_catalog.regclass
          AND attname = 'attempt_phase'
          AND attnum > 0
          AND NOT attisdropped
          AND atttypid = 'pg_catalog.text'::pg_catalog.regtype
          AND atttypmod = -1
          AND attndims = 0
          AND NOT attnotnull
          AND NOT atthasdef
          AND attidentity = ''
          AND attgenerated = ''
    ) THEN
        RAISE EXCEPTION 'result_receipts.attempt_phase schema postcondition failed';
    END IF;
END;
$receipt_attempt_phase$;
