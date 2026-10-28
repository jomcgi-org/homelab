-- No seed data or activation. The enabled job creates the singleton lazily.
CREATE TABLE stars.hour_archive_checkpoint (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    payload TEXT,
    CHECK (payload IS NULL OR octet_length(payload) <= 1048576)
);
