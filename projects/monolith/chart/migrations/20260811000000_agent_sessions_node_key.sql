ALTER TABLE agent_sessions.agent_sessions
  ADD COLUMN node_key TEXT,
  ADD COLUMN node_attempt INTEGER;

CREATE INDEX agent_sessions_workflow_node_idx
  ON agent_sessions.agent_sessions (workflow_id, node_key, node_attempt)
  WHERE workflow_id IS NOT NULL;

-- Backfill the two historical suffix grammars. Anything else stays NULL.
UPDATE agent_sessions.agent_sessions
SET node_key = 'implement',
    node_attempt = (regexp_match(local_session_id, '-implement-(\d+)$'))[1]::int
WHERE workflow_id IS NOT NULL
  AND local_session_id ~ '-implement-\d+$';

UPDATE agent_sessions.agent_sessions
SET node_key = 'review', node_attempt = 1
WHERE workflow_id IS NOT NULL
  AND local_session_id LIKE '%-review';
