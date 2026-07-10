CREATE SCHEMA IF NOT EXISTS demo;

CREATE TABLE demo.load_run (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workload      text NOT NULL,
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    duration_s    integer NOT NULL DEFAULT 120,
    status        text NOT NULL DEFAULT 'running',
    config        jsonb NOT NULL,
    summary       jsonb
);

CREATE TABLE demo.load_scan (
    id             bigserial PRIMARY KEY,
    run_id         uuid NOT NULL REFERENCES demo.load_run(id) ON DELETE CASCADE,
    workload       text NOT NULL,
    seq            integer NOT NULL,
    name           text NOT NULL,
    status         text NOT NULL,
    latency_ms     integer NOT NULL,
    queue_wait_ms  integer,
    cpu_ms         integer,
    peak_rss_mib   integer,
    result_count   integer,
    result         jsonb,
    error          text,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_load_scan_run ON demo.load_scan(run_id, seq);
