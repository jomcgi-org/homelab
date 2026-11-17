-- Session-local report input, not an itsaplan migration or persistent write.
CREATE TEMP TABLE connector_run_usage (
    run_id bigint PRIMARY KEY,
    issue_id bigint NOT NULL,
    origin text NOT NULL,
    input_tokens bigint CHECK (input_tokens >= 0),
    output_tokens bigint CHECK (output_tokens >= 0),
    CHECK ((origin = 'itsaplan' AND run_id > 0)
           OR (origin = 'connector' AND run_id < 0))
);
