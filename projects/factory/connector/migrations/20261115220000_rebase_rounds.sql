-- Main moving is independent of review failures. Persist the separate bound
-- and a synthetic conflict event per PR head in the existing event ledger.
ALTER TABLE work_item
    ADD COLUMN rebase_rounds integer NOT NULL DEFAULT 0 CHECK (rebase_rounds >= 0);

ALTER TABLE review_event DROP CONSTRAINT review_event_kind_check;
ALTER TABLE review_event ADD CONSTRAINT review_event_kind_check
    CHECK (kind IN ('changes_requested', 'check_failed', 'status_failed', 'conflicting'));
