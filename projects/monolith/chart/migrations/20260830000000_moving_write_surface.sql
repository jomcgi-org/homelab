-- Moving planner write surface: leave spans and collision acknowledgements.
--
-- Vocabularies use TEXT + CHECK rather than PostgreSQL enum types because
-- ALTER TYPE does not roll back cleanly inside a transaction, and these
-- vocabularies will change as the move progresses.

-- The original CHECK was unnamed inline, so PostgreSQL auto-named it
-- spans_kind_check; re-add under the same name.
ALTER TABLE moving.spans
    DROP CONSTRAINT spans_kind_check;

ALTER TABLE moving.spans
    ADD CONSTRAINT spans_kind_check
    CHECK (kind IN ('visitor', 'work', 'move', 'trip', 'leave'));

CREATE TABLE moving.collision_acks (
    item1_id   UUID NOT NULL,
    item2_id   UUID NOT NULL,
    note       TEXT,
    acked_by   TEXT NOT NULL CHECK (acked_by IN ('joe', 'anna')),
    acked_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (item1_id, item2_id),
    CHECK (item1_id < item2_id)
);
