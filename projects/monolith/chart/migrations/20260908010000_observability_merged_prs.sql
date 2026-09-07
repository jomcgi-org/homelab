-- #5898: snapshot merged pull requests

CREATE TABLE observability.merged_prs (
    number          INTEGER PRIMARY KEY,
    title           TEXT NOT NULL,
    merged_at       TIMESTAMPTZ NOT NULL,
    additions       INTEGER NOT NULL,
    deletions       INTEGER NOT NULL,
    changed_files   INTEGER NOT NULL,
    type            TEXT NOT NULL,
    scope           TEXT,
    agent_authored  BOOLEAN NOT NULL,
    snapshotted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT merged_prs_type_check CHECK (
        type IN ('feat', 'fix', 'docs', 'chore', 'test', 'refactor', 'other')
    )
);

CREATE INDEX merged_prs_merged_at_idx
    ON observability.merged_prs (merged_at);

GRANT SELECT ON observability.merged_prs TO public_reader;
