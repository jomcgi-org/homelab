ALTER TABLE swarm.work_item
  ADD COLUMN github_pointer_comment_id BIGINT,
  ADD COLUMN pointer_synced_version INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN pointer_synced_at TIMESTAMPTZ,
  ADD COLUMN pointer_failures INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN pointer_next_attempt_at TIMESTAMPTZ;
