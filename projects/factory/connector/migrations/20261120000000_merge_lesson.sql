/* invariant: FACT-13 */
CREATE TABLE merge_lesson (
    work_item_id bigint NOT NULL REFERENCES work_item(id) ON DELETE CASCADE,
    head_sha text NOT NULL,
    lesson text NOT NULL,
    PRIMARY KEY (work_item_id, head_sha)
);
ALTER TABLE outbox DROP CONSTRAINT outbox_kind_check;
ALTER TABLE outbox ADD CONSTRAINT outbox_kind_check
    CHECK (kind IN ('result', 'comment', 'issue_done', 'deferred_start', 'activation', 'retain'));
