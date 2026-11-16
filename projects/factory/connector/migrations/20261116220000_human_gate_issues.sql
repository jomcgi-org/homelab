-- Human-gate issues are independent of the coding work item's state. Keep
-- polling during revisions and after a stop, until a terminal move is delivered.
ALTER TABLE pull_request ADD COLUMN human_gate_polled_at timestamptz;

CREATE TABLE human_gate_issue (
    id bigserial PRIMARY KEY,
    repository text NOT NULL,
    number integer NOT NULL,
    pull_request_id bigint NOT NULL REFERENCES pull_request (id),
    issue_id bigint,
    head_sha text NOT NULL,
    state text NOT NULL CHECK (state IN ('Todo', 'Backlog', 'Done', 'Canceled')),
    description text NOT NULL,
    revision integer NOT NULL DEFAULT 1,
    applied_revision integer NOT NULL DEFAULT 0,
    UNIQUE (repository, number)
);

-- Immutable payloads and message IDs precede every itsaplan write. A create
-- whose send started is only searched for, never blindly repeated. Updates
-- and the move of a duplicate issue to Canceled set absolute values and may
-- be repeated.
CREATE TABLE human_gate_attempt (
    message_id text PRIMARY KEY,
    gate_id bigint NOT NULL REFERENCES human_gate_issue (id),
    operation text NOT NULL CHECK (operation IN ('create', 'update', 'cancel')),
    revision integer NOT NULL,
    payload jsonb NOT NULL,
    writer_epoch bigint NOT NULL,
    sends integer NOT NULL DEFAULT 0,
    send_started boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    attempted_at timestamptz,
    finished_at timestamptz
);
