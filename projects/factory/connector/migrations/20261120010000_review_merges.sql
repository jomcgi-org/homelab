/* invariant: FACT-13 */
ALTER TABLE repo_binding ADD COLUMN review_merges_since timestamptz NOT NULL DEFAULT date_trunc('second', now());

ALTER TABLE outbox DROP CONSTRAINT outbox_kind_check;
ALTER TABLE outbox ADD CONSTRAINT outbox_kind_check
    CHECK (kind IN ('result', 'comment', 'issue_done', 'deferred_start', 'activation', 'review_followup'));
