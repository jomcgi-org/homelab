-- Identify the actor incarnation and revision seen after the pod disappeared.
ALTER TABLE worker_fence ADD COLUMN actor_uid text, ADD COLUMN actor_version bigint;
-- Reobserve evidence written before the actor revision was recorded.
UPDATE worker_fence SET gone_at = NULL WHERE gone_at IS NOT NULL;
ALTER TABLE worker_fence ADD CHECK (gone_at IS NULL OR
    (actor_uid IS NOT NULL AND actor_uid <> '' AND actor_version IS NOT NULL AND actor_version > 0));
