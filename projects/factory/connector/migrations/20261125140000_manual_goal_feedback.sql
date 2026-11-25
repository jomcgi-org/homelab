-- HOME-738. Keep every kind that 20261125120000_assessment.sql allows.
ALTER TABLE outbox DROP CONSTRAINT outbox_kind_check;
ALTER TABLE outbox ADD CONSTRAINT outbox_kind_check
    CHECK (kind IN ('result', 'comment', 'issue_done', 'deferred_start', 'activation', 'issue_reviewing', 'retain', 'review_followup', 'assessment', 'manual_goal_feedback'));
