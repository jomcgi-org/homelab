-- Moving planner schema: shared tasks, events, date ranges, roles, and viewers.
--
-- Vocabularies use TEXT + CHECK rather than PostgreSQL enum types because
-- ALTER TYPE does not roll back cleanly inside a transaction, and these
-- vocabularies will change as the move progresses.

CREATE SCHEMA IF NOT EXISTS moving;

CREATE TABLE moving.tasks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    track       TEXT CHECK (track IN ('sell', 'admin', 'ship', 'people')),
    title       TEXT NOT NULL,
    note        TEXT,
    owner       TEXT CHECK (owner IN ('joe', 'anna', 'both')),
    due_on      DATE,
    done_at     TIMESTAMPTZ,
    value_cad   NUMERIC,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON COLUMN moving.tasks.owner IS
    'owner=''both'' is one shared row with shared done-ness, never two rows';

CREATE TABLE moving.milestones (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title            TEXT NOT NULL,
    occurs_on        DATE NOT NULL,
    owner            TEXT CHECK (owner IN ('joe', 'anna', 'both')),
    gcal_event_id    TEXT,
    gcal_synced_at   TIMESTAMPTZ,
    gcal_state       TEXT NOT NULL DEFAULT 'queued'
                     CHECK (gcal_state IN ('queued', 'synced', 'held'))
);

CREATE TABLE moving.spans (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind        TEXT CHECK (kind IN ('visitor', 'work', 'move', 'trip')),
    label       TEXT NOT NULL,
    starts_on   DATE NOT NULL,
    ends_on     DATE NOT NULL,
    CHECK (ends_on >= starts_on)
);

CREATE TABLE moving.roles (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company     TEXT NOT NULL,
    title       TEXT NOT NULL,
    stage       TEXT CHECK (stage IN ('applied', 'screen', 'onsite', 'offer', 'closed')),
    next_on     DATE,
    span_id     UUID REFERENCES moving.spans(id) ON DELETE SET NULL
);

CREATE TABLE moving.viewers (
    email       TEXT PRIMARY KEY,
    name        TEXT NOT NULL CHECK (name IN ('joe', 'anna'))
);

COMMENT ON TABLE moving.viewers IS
    'Email lookup only. Rows are seeded out of band so addresses are never committed to Git.';
