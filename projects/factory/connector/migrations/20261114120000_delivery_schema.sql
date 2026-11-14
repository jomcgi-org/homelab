-- Connector delivery (projects/factory/ARCHITECTURE.md, "Connector"): the one
-- PR per work item, review rounds, the merge queue and the merge.

-- A run the connector starts itself: a review round. itsaplan has no record
-- of it, so nothing about it goes to the runner API. Its ids are negative,
-- from connector_run_id, and never meet itsaplan's.
ALTER TABLE run ADD COLUMN origin text NOT NULL DEFAULT 'itsaplan'
    CHECK (origin IN ('itsaplan', 'connector'));
CREATE SEQUENCE connector_run_id AS bigint INCREMENT BY -1 MAXVALUE -1 START WITH -1;

-- Context a replacement session starts from, captured once so every send of
-- the same message carries the same input.
ALTER TABLE delivery_attempt ADD COLUMN context text;

-- review_rounds counts connector review rounds against the binding's bound.
-- turn_attempt_id is the completed turn the PR must reflect; synced_attempt_id
-- the last one it does. A stopped work item gets no PR updates or review
-- rounds, and its running round is canceled, until a new itsaplan run
-- resumes it; stops counts how often, so each stop is announced.
ALTER TABLE work_item
    ADD COLUMN review_rounds integer NOT NULL DEFAULT 0,
    ADD COLUMN turn_attempt_id bigint REFERENCES delivery_attempt (id),
    ADD COLUMN synced_attempt_id bigint REFERENCES delivery_attempt (id),
    ADD COLUMN stopped_reason text,
    ADD COLUMN stops integer NOT NULL DEFAULT 0;
ALTER TABLE work_item DROP CONSTRAINT work_item_state_check;
ALTER TABLE work_item ADD CONSTRAINT work_item_state_check
    CHECK (state IN ('active', 'blocked', 'review', 'merging', 'done', 'stopped'));
CREATE INDEX work_item_delivery_idx ON work_item (state) WHERE state IN ('review', 'merging', 'done');

-- round_stopped cancels a review round whose work item stopped delivery.
ALTER TABLE cancellation DROP CONSTRAINT cancellation_reason_check;
ALTER TABLE cancellation ADD CONSTRAINT cancellation_reason_check
    CHECK (reason IN ('run_canceled', 'run_closed', 'reassignment', 'round_stopped'));

-- enqueued_head_sha is the head the connector put in the merge queue;
-- ejected_head_sha the last head the queue sent back, never enqueued again.
ALTER TABLE pull_request
    ADD COLUMN url text NOT NULL DEFAULT '',
    ADD COLUMN polled_at timestamptz,
    ADD COLUMN enqueued_head_sha text,
    ADD COLUMN ejected_head_sha text,
    ADD COLUMN merge_commit_sha text,
    ADD COLUMN merged_at timestamptz;

-- source is the reviewer login or the check name; a round acts once per
-- head, kind and source. round_run_id is the review round an event started.
ALTER TABLE review_event
    ADD COLUMN source text NOT NULL DEFAULT '',
    ADD COLUMN round_run_id bigint REFERENCES run (id),
    ADD CONSTRAINT review_event_kind_check
        CHECK (kind IN ('changes_requested', 'check_failed', 'status_failed')),
    ADD CONSTRAINT review_event_disposition_check
        CHECK (disposition IN ('acted', 'stale_head', 'untrusted', 'duplicate', 'round_limit'));
CREATE INDEX review_event_acted_idx ON review_event (pull_request_id, commit_sha, kind, source)
    WHERE disposition = 'acted';

-- issue_done moves the itsaplan issue to its project's completed column once
-- the merge is observed.
ALTER TABLE outbox DROP CONSTRAINT outbox_kind_check;
ALTER TABLE outbox ADD CONSTRAINT outbox_kind_check CHECK (kind IN ('result', 'comment', 'issue_done'));
