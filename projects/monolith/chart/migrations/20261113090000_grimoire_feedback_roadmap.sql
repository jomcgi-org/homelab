-- The feedback roadmap: every signed-in user sees what was suggested and what
-- happened to it. Rows written before this migration stay unshared: their
-- senders wrote them as private notes. No public_reader grant on either table.
ALTER TABLE grimoire.feedback
    ADD COLUMN shared BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN title TEXT,
    ADD COLUMN reason TEXT,
    ADD COLUMN issue_state TEXT,
    ADD COLUMN issue_checked_at TIMESTAMPTZ,
    ADD CONSTRAINT feedback_title_chk CHECK (length(title) BETWEEN 1 AND 120),
    ADD CONSTRAINT feedback_reason_chk CHECK (length(reason) BETWEEN 1 AND 200),
    ADD CONSTRAINT feedback_issue_state_chk
        CHECK (issue_state IN ('open', 'completed', 'not_planned'));

-- One row per voter and item: "me too" from someone other than the author.
CREATE TABLE grimoire.feedback_vote (
    feedback_id UUID NOT NULL REFERENCES grimoire.feedback(id) ON DELETE CASCADE,
    voter_email TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (feedback_id, voter_email)
);
