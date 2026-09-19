-- Durable health signal for agent report persistence failures.

CREATE TABLE knowledge.agent_report_write_failures (
    id             BIGSERIAL   PRIMARY KEY,
    reporter_kind  TEXT        NOT NULL,
    error_type     TEXT        NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX agent_report_write_failures_created_at
    ON knowledge.agent_report_write_failures (created_at DESC);

GRANT SELECT, INSERT ON knowledge.agent_report_write_failures TO agents_writer;
GRANT USAGE, SELECT ON SEQUENCE knowledge.agent_report_write_failures_id_seq
    TO agents_writer;
