-- chat.whatsapp_reminder: ad-hoc reminders created in the household group (ADR
-- 039 spec section 5d). A reminder is created conversationally ("remind us to X
-- on Y"); the morning digest renders open (undelivered, due) reminders and stamps
-- delivered_at as it includes them, so a reminder surfaces once. due_at is stored
-- in UTC; created_by records the participant who set it. The partial-shaped index
-- serves the digest's "open reminders due by now" scan per group.
CREATE TABLE chat.whatsapp_reminder (
    id           BIGSERIAL PRIMARY KEY,
    group_jid    TEXT NOT NULL,
    text         TEXT NOT NULL,
    due_at       TIMESTAMPTZ NOT NULL,
    created_by   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at TIMESTAMPTZ
);

CREATE INDEX whatsapp_reminder_group_open_idx
    ON chat.whatsapp_reminder (group_jid, delivered_at, due_at);
