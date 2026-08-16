-- Retire sandbox.session (ADR agents/057, issue #4981).
--
-- This table mapped a caller-chosen handle to an EmberVM session id plus its
-- per-session capability token, backing the sessioned variant of run_python.
-- Sessioned execution is gone: the tool is now run_code, every run is one-shot,
-- and nothing in the monolith reads or writes this table any more (models.py
-- and repository.py were deleted in the same change).
--
-- Dropping it also destroys the stored capability tokens, which is the point:
-- they are secrets with no remaining consumer, and a credential nobody uses is
-- still a credential someone could steal.
--
-- RESTRICT, never CASCADE. RESTRICT is the default and makes this fail loudly
-- if anything unexpected was created in this schema, rather than silently
-- destroying an object nobody remembered. `session` is the only table this
-- schema ever held, so the expected outcome is a clean drop.

DROP TABLE IF EXISTS sandbox.session;

DROP SCHEMA IF EXISTS sandbox RESTRICT;
