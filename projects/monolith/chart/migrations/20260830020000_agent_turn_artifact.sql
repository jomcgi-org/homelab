-- The declared-artifact channel, issue #5425. A conductor-dispatched node
-- declares one artifact file per turn; the guest shim reads that file directly
-- and delivers it beside the turn diff, so a huge work diff can no longer cost
-- the caller its small declared document (the livelock #5426 mitigated). The
-- blob is the raw file bytes; outcome records why a blob is absent
-- (missing / oversize / invalid_path / unreadable). Nothing writes these
-- columns until the conductor (#5419) declares an artifact.
ALTER TABLE agent_sessions.agent_turns ADD COLUMN artifact_path TEXT;
ALTER TABLE agent_sessions.agent_turns ADD COLUMN artifact_blob BYTEA;
ALTER TABLE agent_sessions.agent_turns ADD COLUMN artifact_outcome TEXT;
