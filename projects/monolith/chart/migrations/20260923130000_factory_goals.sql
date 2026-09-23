-- #5927: orchestrator-declared factory goals.
--
-- The /slop/factory goal panel used to infer intent from merged PRs. Goals
-- are intent, so the orchestrator declares them here instead: a one-line
-- statement, the linked issue numbers, who declared it, and when. At most a
-- handful of rows are active at once; replacing the set deactivates the old
-- rows rather than deleting them, so declaration history survives.
-- Progress is scored deterministically at read time against
-- observability.merged_prs, never narrated.

CREATE TABLE observability.factory_goals (
    id              SERIAL PRIMARY KEY,
    statement       TEXT NOT NULL,
    issue_numbers   INTEGER[] NOT NULL DEFAULT '{}',
    declared_by     TEXT NOT NULL,
    declared_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    active          BOOLEAN NOT NULL DEFAULT true
);

CREATE INDEX factory_goals_active_idx
    ON observability.factory_goals (active) WHERE active;

GRANT SELECT ON observability.factory_goals TO public_reader;
