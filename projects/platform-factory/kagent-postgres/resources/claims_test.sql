-- Run in a disposable cluster as a superuser.
CREATE DATABASE claims_test;
\connect claims_test
\setenv KAGENT_OWNER_PASSWORD test_owner
\setenv KAGENT_READWRITE_PASSWORD test_writer
BEGIN;
\ir setup.psql
COMMIT;

SET ROLE pg_monitor;
DO $$ BEGIN
  IF EXISTS (SELECT FROM kagent.pending_claims()) THEN
    RAISE EXCEPTION 'fresh database returned claims';
  END IF;
END $$;
RESET ROLE;

SET ROLE kagent_owner;
CREATE TABLE kagent.session (id text, history_id int);
CREATE TABLE kagent.session_task_event
  (history_id int, quiescence_executor_id text, quiescence_pending boolean);
INSERT INTO kagent.session VALUES ('locked', 1), ('idle', 2);
INSERT INTO kagent.session_task_event VALUES
  (1, 'worker', true), (1, 'worker', true), (2, 'other', false), (2, NULL, true);
RESET ROLE;
BEGIN;
\ir setup.psql
COMMIT;

SET ROLE pg_monitor;
DO $$ BEGIN
  IF (SELECT count(*) FROM kagent.pending_claims()) <> 1 OR NOT EXISTS
    (SELECT FROM kagent.pending_claims() WHERE session_id = 'locked' AND executor_id = 'worker') THEN
    RAISE EXCEPTION 'unexpected claim pairs';
  END IF;
END $$;
RESET ROLE;

-- Give schema access so the denial specifically exercises function EXECUTE.
CREATE ROLE claims_reader;
GRANT USAGE ON SCHEMA kagent TO claims_reader;
SET ROLE claims_reader;
DO $$ BEGIN
  PERFORM * FROM kagent.pending_claims();
  RAISE EXCEPTION 'ungranted role could execute pending_claims';
EXCEPTION WHEN insufficient_privilege THEN NULL;
END $$;
RESET ROLE;
