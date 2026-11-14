-- Connector state for dispatch (projects/factory/ARCHITECTURE.md, "Connector").
-- itsaplan owns requests and runs, kagent owns execution and sessions, GitHub
-- owns PR state; these tables record how they relate.

-- The single writer. Each takeover of the advisory lock increments epoch, and
-- every external side effect checks it first.
CREATE TABLE writer (
    id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    epoch bigint NOT NULL,
    holder text NOT NULL,
    acquired_at timestamptz NOT NULL DEFAULT now()
);

-- One repository binding: the access boundary. Repository and branch come only
-- from here, never from a run's prompt.
CREATE TABLE repo_binding (
    name text PRIMARY KEY,
    owner text NOT NULL,
    repo text NOT NULL,
    base_branch text NOT NULL,
    itsaplan_project text NOT NULL UNIQUE,
    kagent_namespace text NOT NULL,
    kagent_agent text NOT NULL,
    branch_prefix text NOT NULL,
    max_engineering_retries integer NOT NULL CHECK (max_engineering_retries >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner, repo)
);

-- One itsaplan issue delivered through one kagent session, whichever agent
-- key delivers its runs. epoch is bumped on agent reassignment; attempts sent
-- under an older epoch stop writing.
CREATE TABLE work_item (
    id bigserial PRIMARY KEY,
    binding_name text NOT NULL REFERENCES repo_binding (name),
    issue_id bigint NOT NULL UNIQUE,
    issue_key text NOT NULL,
    branch text NOT NULL,
    agent_key text NOT NULL,
    kagent_namespace text NOT NULL,
    kagent_agent text NOT NULL,
    session_id text,
    session_generation integer NOT NULL DEFAULT 0,
    epoch bigint NOT NULL DEFAULT 1,
    engineering_retries integer NOT NULL DEFAULT 0,
    state text NOT NULL DEFAULT 'active'
        CHECK (state IN ('active', 'blocked', 'review', 'done')),
    blocked_class text CHECK (blocked_class IN ('credential', 'quota')),
    blocked_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- One itsaplan run, keyed by its id. attempt is the itsaplan attempt this
-- connector last claimed; heartbeats, events and results always send it.
CREATE TABLE run (
    id bigint PRIMARY KEY,
    agent_key text NOT NULL,
    work_item_id bigint REFERENCES work_item (id),
    issue_id bigint,
    issue_key text,
    trigger text NOT NULL,
    prompt text NOT NULL,
    system_prompt text NOT NULL,
    attempt integer NOT NULL CHECK (attempt >= 1),
    state text NOT NULL
        CHECK (state IN ('claimed', 'waiting', 'dispatched', 'finishing', 'canceling', 'stale', 'closed')),
    event_attempt integer,
    event_offset integer NOT NULL DEFAULT 0,
    -- The task whose history is being posted, and the offset where it began.
    event_task_id text,
    event_task_base integer NOT NULL DEFAULT 0,
    last_heartbeat_at timestamptz,
    closed_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX run_open_idx ON run (state) WHERE state <> 'closed';

-- One send to kagent. The message ID is recorded before the send, so a lost
-- response is reconciled by finding the task that holds it.
CREATE TABLE delivery_attempt (
    id bigserial PRIMARY KEY,
    run_id bigint NOT NULL REFERENCES run (id),
    seq integer NOT NULL CHECK (seq >= 1),
    run_attempt integer NOT NULL,
    work_item_epoch bigint NOT NULL,
    decision_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('initial', 'continuation')),
    message_id text NOT NULL UNIQUE,
    kagent_namespace text NOT NULL,
    kagent_agent text NOT NULL,
    context_id text,
    task_id text,
    state text NOT NULL
        CHECK (state IN ('sending', 'ambiguous', 'accepted', 'completed', 'failed', 'canceled')),
    error_class text
        CHECK (error_class IN ('engineering', 'credential', 'quota', 'canceled')),
    error text,
    evidence_uri text,
    writer_epoch bigint NOT NULL,
    sent_at timestamptz,
    finished_at timestamptz,
    UNIQUE (run_id, seq)
);

CREATE INDEX delivery_attempt_open_idx ON delivery_attempt (run_id)
    WHERE state IN ('sending', 'ambiguous', 'accepted');

-- The one PR per work item. Logic arrives with connector delivery (step 10).
CREATE TABLE pull_request (
    id bigserial PRIMARY KEY,
    work_item_id bigint NOT NULL UNIQUE REFERENCES work_item (id),
    number integer NOT NULL,
    node_id text NOT NULL UNIQUE,
    head_sha text NOT NULL,
    merge_queue_state text,
    state text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Review events seen on owned PRs. Logic arrives with step 10.
CREATE TABLE review_event (
    id bigserial PRIMARY KEY,
    pull_request_id bigint NOT NULL REFERENCES pull_request (id),
    github_node_id text NOT NULL UNIQUE,
    commit_sha text NOT NULL,
    author text NOT NULL,
    kind text NOT NULL,
    disposition text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- A requested kagent task cancellation. It stays pending until kagent reports
-- the task in a terminal state.
CREATE TABLE cancellation (
    id bigserial PRIMARY KEY,
    delivery_attempt_id bigint NOT NULL REFERENCES delivery_attempt (id),
    run_id bigint NOT NULL REFERENCES run (id),
    reason text NOT NULL CHECK (reason IN ('run_canceled', 'reassignment')),
    state text NOT NULL CHECK (state IN ('pending', 'confirmed')),
    requests integer NOT NULL DEFAULT 0,
    outcome text,
    requested_at timestamptz NOT NULL DEFAULT now(),
    confirmed_at timestamptz
);

CREATE UNIQUE INDEX cancellation_pending_uq ON cancellation (delivery_attempt_id)
    WHERE state = 'pending';

-- Side effects recorded in the same transaction as the state change that
-- causes them, then delivered by the writer.
CREATE TABLE outbox (
    id bigserial PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('result', 'comment')),
    dedupe_key text NOT NULL UNIQUE,
    run_id bigint REFERENCES run (id),
    run_attempt integer,
    agent_key text,
    payload jsonb NOT NULL,
    tries integer NOT NULL DEFAULT 0,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    delivered_at timestamptz,
    abandoned_at timestamptz
);

CREATE INDEX outbox_pending_idx ON outbox (id)
    WHERE delivered_at IS NULL AND abandoned_at IS NULL;
