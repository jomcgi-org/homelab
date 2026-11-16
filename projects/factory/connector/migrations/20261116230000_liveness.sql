-- The liveness lease (HOME-141): a dispatched turn that shows no task within
-- intervals.taskLease of its send, or no task event or open tool call for
-- intervals.progressLease, gets a lease_expired cancellation.
ALTER TABLE cancellation DROP CONSTRAINT cancellation_reason_check;
ALTER TABLE cancellation ADD CONSTRAINT cancellation_reason_check
    CHECK (reason IN ('run_canceled', 'run_closed', 'reassignment', 'round_stopped', 'quota_stall', 'lease_expired'));
-- Its confirmation closes the attempt as abandoned; the run then takes an
-- engineering retry in a fresh session.
ALTER TABLE delivery_attempt DROP CONSTRAINT delivery_attempt_error_class_check;
ALTER TABLE delivery_attempt ADD CONSTRAINT delivery_attempt_error_class_check
    CHECK (error_class IN ('engineering', 'credential', 'quota', 'canceled', 'abandoned'));
-- A cancellation kagent has not confirmed within intervals.forceDelete
-- deletes the attempt's session with force (reason stuck). force records
-- that the connector sent DeleteSession with force; reverts counts the
-- RevertSession calls sent after kagent refused it. Each call is recorded
-- here under the writer epoch before it is sent.
ALTER TABLE session_deletion DROP CONSTRAINT session_deletion_reason_check;
ALTER TABLE session_deletion ADD CONSTRAINT session_deletion_reason_check
    CHECK (reason IN ('done', 'stopped', 'replaced', 'stuck'));
ALTER TABLE session_deletion
    ADD COLUMN force boolean NOT NULL DEFAULT false,
    ADD COLUMN reverts integer NOT NULL DEFAULT 0;
