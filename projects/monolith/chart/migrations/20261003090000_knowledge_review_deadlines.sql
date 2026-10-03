ALTER TABLE knowledge.notes
    ADD COLUMN review_after timestamptz,
    ADD COLUMN review_policy text,
    ADD COLUMN last_reviewed_at timestamptz,
    ADD CONSTRAINT notes_review_deadline_chk CHECK (
        review_after IS NULL OR (
            COALESCE(last_reviewed_at, observed_at) IS NOT NULL
            AND review_after <= COALESCE(last_reviewed_at, observed_at) + INTERVAL '2160 hours'
        )
    );

CREATE INDEX notes_review_after_idx ON knowledge.notes (review_after, id)
    WHERE deleted_at IS NULL;
