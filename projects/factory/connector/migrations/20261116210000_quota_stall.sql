-- quota_stall cancels a turn that went quiet while the broker reads the
-- Codex quota exhausted (HOME-127). event_count and event_at record when the
-- connector last saw the attempt's kagent task change; tool_running records
-- that a tool call in it had no result then.
ALTER TABLE cancellation DROP CONSTRAINT cancellation_reason_check;
ALTER TABLE cancellation ADD CONSTRAINT cancellation_reason_check
    CHECK (reason IN ('run_canceled', 'run_closed', 'reassignment', 'round_stopped', 'quota_stall'));
-- A quota_stall cancellation records the task size it was decided on
-- (event_count). A read before its first CancelTask that shows the task
-- grew or has an open tool call withdraws it instead.
ALTER TABLE cancellation DROP CONSTRAINT cancellation_state_check;
ALTER TABLE cancellation ADD CONSTRAINT cancellation_state_check
    CHECK (state IN ('pending', 'confirmed', 'withdrawn'));
ALTER TABLE cancellation ADD COLUMN event_count integer;
ALTER TABLE delivery_attempt
    ADD COLUMN event_count integer NOT NULL DEFAULT 0,
    ADD COLUMN event_at timestamptz,
    ADD COLUMN tool_running boolean NOT NULL DEFAULT false;

-- A stable message identifies each broker quota read across polls and
-- writer takeovers: one per quota block or quiet turn the read serves. The
-- read is idempotent and always fetches the broker's current reading.
CREATE TABLE quota_read (
    message_id text PRIMARY KEY,
    writer_epoch bigint NOT NULL,
    attempts integer NOT NULL DEFAULT 1,
    attempted_at timestamptz NOT NULL DEFAULT now()
);
