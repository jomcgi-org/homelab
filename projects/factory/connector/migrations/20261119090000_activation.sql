-- One follow-up per work item, saved before delegation so a restart reuses it.
CREATE TABLE activation_followup (
    work_item_id bigint PRIMARY KEY REFERENCES work_item (id),
    issue_id bigint NOT NULL UNIQUE,
    issue_key text NOT NULL
);

ALTER TABLE outbox DROP CONSTRAINT outbox_kind_check;
ALTER TABLE outbox ADD CONSTRAINT outbox_kind_check
    CHECK (kind IN ('result', 'comment', 'issue_done', 'deferred_start', 'activation'));
