-- Restore a fact's actual verification state when a dispute is rejected.
-- Nullable preserves safe behavior for disputes created before this snapshot
-- was captured: those rows do not guess a prior state during resolution.
ALTER TABLE knowledge.disputes
    ADD COLUMN previous_verification_state TEXT,
    ADD CONSTRAINT disputes_previous_verification_state_chk CHECK (
        previous_verification_state IS NULL
        OR previous_verification_state IN (
            'legacy', 'unverified', 'verified', 'disputed', 'invalidated'
        )
    );
