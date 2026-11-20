CREATE TABLE assessment (
    work_id bigint PRIMARY KEY REFERENCES work_item(id),
    parent_id bigint NOT NULL,
    child_id bigint UNIQUE,
    days integer NOT NULL,
    measure text NOT NULL,
    issue_url text NOT NULL,
    pr_url text NOT NULL,
    verdict text
);

ALTER TABLE outbox DROP CONSTRAINT outbox_kind_check;
ALTER TABLE outbox ADD CONSTRAINT outbox_kind_check
    CHECK (kind IN ('result', 'comment', 'issue_done', 'deferred_start', 'activation', 'assessment'));
