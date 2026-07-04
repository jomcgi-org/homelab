-- Ambient-assistant reminders (Phase 2). A chat tool inserts a row with
-- due_at in the future; the scheduler drain job queries pending rows whose
-- due_at has passed, posts them into chat.discord_outbox, then stamps
-- delivered_at and flips status to 'delivered'. A user can cancel a
-- still-pending reminder before it fires (status='cancelled'). The status
-- CHECK is mirrored in the SQLModel __table_args__ so the SQLite test
-- fixtures enforce it too (create_all does not see migration-only
-- constraints).
CREATE TABLE IF NOT EXISTS chat.reminder (
    id BIGSERIAL PRIMARY KEY,
    channel_id TEXT NOT NULL,
    author_id TEXT NOT NULL,
    content TEXT NOT NULL,
    due_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delivered_at TIMESTAMPTZ,
    CONSTRAINT reminder_status_valid CHECK (status IN ('pending', 'delivered', 'cancelled'))
);
-- Drain query: pending reminders whose due_at has passed, oldest first.
CREATE INDEX IF NOT EXISTS reminder_status_due_at ON chat.reminder (status, due_at);
