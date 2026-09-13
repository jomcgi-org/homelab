-- Durable Discord scheduled tasks and occurrence-safe proactive delivery.
--
-- A scheduled task owns the next due instant. Claiming persists a deterministic
-- occurrence identity before async work starts; finalization atomically writes
-- one outbox row and completes or advances the task. Expired claim leases can be
-- recovered after a crash without inventing another occurrence.
CREATE TABLE chat.scheduled_tasks (
    id BIGSERIAL PRIMARY KEY,
    channel_id TEXT NOT NULL,
    author_id TEXT NOT NULL,
    task_kind TEXT NOT NULL,
    schedule_kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    due_at TIMESTAMPTZ,
    cron_expression TEXT,
    next_run_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    claim_token TEXT,
    claimed_at TIMESTAMPTZ,
    current_occurrence_id TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT scheduled_task_kind_valid
        CHECK (task_kind IN ('reminder', 'digest')),
    CONSTRAINT scheduled_task_schedule_kind_valid
        CHECK (schedule_kind IN ('one_shot', 'cron')),
    CONSTRAINT scheduled_task_status_valid
        CHECK (status IN ('pending', 'claimed', 'completed', 'failed', 'cancelled')),
    CONSTRAINT scheduled_task_shape_valid CHECK (
        (schedule_kind = 'one_shot' AND task_kind = 'reminder' AND due_at IS NOT NULL
            AND cron_expression IS NULL)
        OR
        (schedule_kind = 'cron' AND task_kind = 'digest' AND due_at IS NULL
            AND cron_expression IS NOT NULL)
    )
);

CREATE INDEX scheduled_tasks_due
    ON chat.scheduled_tasks (next_run_at, id)
    WHERE status = 'pending';
CREATE INDEX scheduled_tasks_stale_claims
    ON chat.scheduled_tasks (claimed_at, id)
    WHERE status = 'claimed';
CREATE INDEX scheduled_tasks_author_active
    ON chat.scheduled_tasks (author_id, next_run_at)
    WHERE status IN ('pending', 'claimed');

CREATE TABLE chat.scheduled_task_occurrences (
    occurrence_id TEXT PRIMARY KEY,
    scheduled_task_id BIGINT NOT NULL REFERENCES chat.scheduled_tasks(id)
        ON DELETE CASCADE,
    scheduled_for TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'claimed',
    claim_token TEXT NOT NULL,
    claimed_at TIMESTAMPTZ NOT NULL,
    outbox_id BIGINT REFERENCES chat.discord_outbox(id),
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    enqueued_at TIMESTAMPTZ,
    CONSTRAINT scheduled_task_occurrence_status_valid
        CHECK (status IN ('claimed', 'enqueued', 'delivered', 'uncertain', 'failed')),
    CONSTRAINT scheduled_task_occurrence_task_time_unique
        UNIQUE (scheduled_task_id, scheduled_for)
);

CREATE INDEX scheduled_task_occurrences_task
    ON chat.scheduled_task_occurrences (scheduled_task_id, scheduled_for);

-- Scheduled messages cross an explicit no-retry boundary before calling
-- Discord. If the process stops between send and acknowledgement, the row is
-- quarantined as uncertain on restart. This chooses observable at-most-once
-- dispatch over silently risking a duplicate and does not claim exactly-once.
ALTER TABLE chat.discord_outbox
    ADD COLUMN IF NOT EXISTS dedupe_key TEXT,
    ADD COLUMN IF NOT EXISTS delivery_state TEXT NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS send_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS uncertain_at TIMESTAMPTZ;

ALTER TABLE chat.discord_outbox
    ADD CONSTRAINT discord_outbox_delivery_state_valid
        CHECK (delivery_state IN ('pending', 'sending', 'posted', 'uncertain'));

CREATE UNIQUE INDEX discord_outbox_dedupe_key
    ON chat.discord_outbox (dedupe_key)
    WHERE dedupe_key IS NOT NULL;
