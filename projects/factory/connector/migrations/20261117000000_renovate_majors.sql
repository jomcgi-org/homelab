-- A source PR is admitted once, including after its issue has finished.
-- Pending/uncertain creates reserve a slot before any external write.
CREATE TABLE renovate_major (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repository text NOT NULL,
    number integer NOT NULL CHECK (number > 0),
    issue_id bigint UNIQUE,
    issue_key text NOT NULL DEFAULT '',
    stopped boolean NOT NULL DEFAULT false,
    closed boolean NOT NULL DEFAULT false,
    UNIQUE (repository, number)
);

-- Stable message IDs and immutable write payloads survive writer takeover.
-- A sent write is reconciled by positive evidence, never replayed on absence.
CREATE TABLE renovate_attempt (
    message_id text PRIMARY KEY,
    payload jsonb NOT NULL,
    writer_epoch bigint NOT NULL,
    state text NOT NULL DEFAULT 'ready' CHECK (state IN ('ready', 'sent', 'done')),
    tries integer NOT NULL DEFAULT 0,
    last_error text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now()
);
