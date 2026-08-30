-- Moving planner: tombstone log for deleted Google Calendar events.
CREATE TABLE moving.gcal_tombstones (
    event_id   TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
