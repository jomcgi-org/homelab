-- #5927: GitHub issue state and merged closing refs for declared factory goals.
-- The snapshot-merged-prs job keeps only issues linked by active goals.
-- Public scoring reads this bounded repository-metadata snapshot, not GitHub.

CREATE TABLE observability.factory_goal_issues (
    number                  INTEGER PRIMARY KEY,
    state                   TEXT NOT NULL,
    closed_at               TIMESTAMPTZ NULL,
    closing_prs             INTEGER[] NOT NULL DEFAULT '{}',
    last_closing_merge_at   TIMESTAMPTZ NULL,
    snapshotted_at          TIMESTAMPTZ NOT NULL
);

GRANT SELECT ON observability.factory_goal_issues TO public_reader;
