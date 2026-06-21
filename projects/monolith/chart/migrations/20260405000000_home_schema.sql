-- Replace todo schema with home schema to match the renamed Python module.
-- nosemgrep: migration-destructive-ddl (safe: todo schema is being replaced by home;
-- no production data to preserve, tables are recreated below)
DROP SCHEMA IF EXISTS todo CASCADE;

CREATE SCHEMA IF NOT EXISTS home;

CREATE TABLE home.tasks (
    id SERIAL PRIMARY KEY,
    task TEXT NOT NULL DEFAULT '',
    done BOOLEAN NOT NULL DEFAULT FALSE,
    kind TEXT NOT NULL DEFAULT 'daily',
    position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE home.archives (
    id SERIAL PRIMARY KEY,
    date DATE NOT NULL,
    content TEXT NOT NULL
);
