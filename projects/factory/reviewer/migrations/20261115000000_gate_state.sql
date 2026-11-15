CREATE TABLE writer (
    id integer PRIMARY KEY CHECK (id = 1),
    epoch bigint NOT NULL,
    holder text NOT NULL,
    acquired_at timestamptz NOT NULL DEFAULT now()
);

-- One atomic snapshot, keyed internally by repository/PR and repository/head.
-- Its existence records completion of the one-time GitHub import.
CREATE TABLE reviewer_state (
    id integer PRIMARY KEY CHECK (id = 1),
    state jsonb NOT NULL
);
