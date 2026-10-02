-- Keep unresolved disputes visible after their extraction retries are exhausted.
ALTER TABLE knowledge.disputes DROP CONSTRAINT disputes_state_chk;
ALTER TABLE knowledge.disputes ADD CONSTRAINT disputes_state_chk
    CHECK (state IN ('open', 'confirmed', 'narrowed', 'superseded',
                    'invalidated', 'rejected', 'resolution_failed'));
