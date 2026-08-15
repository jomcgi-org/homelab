-- Capture each turn's diff at completion instead of deriving it from the
-- GitHub compare API at render time. The GitHub path expires with the branch,
-- truncates at 300 files, and cannot see work that was never pushed, so across
-- 191 sessions it never once produced a diff.
--
-- diff_blob holds a zlib-compressed unified diff, so it stays small enough to
-- live beside the turn (typically single-digit KB). diff_truncated records that
-- the guest refused to store an oversized diff, which is a different fact from
-- there being no diff at all: null blob with the flag set means "too big to
-- keep", null blob without it means "nothing captured".
ALTER TABLE agent_sessions.agent_turns ADD COLUMN diff_blob BYTEA;
ALTER TABLE agent_sessions.agent_turns ADD COLUMN diff_truncated BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE agent_sessions.agent_turns ADD COLUMN diff_base_sha TEXT;
