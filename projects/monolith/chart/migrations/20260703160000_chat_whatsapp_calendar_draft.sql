-- chat.whatsapp_calendar_draft: a proposed calendar event awaiting manual
-- confirmation (ADR 039 spec section 5b fallback). When the cluster-side calendar
-- credential is absent at runtime, a scheduling intent is drafted here instead of
-- created live, and the morning digest surfaces open drafts (confirmed_at IS NULL)
-- so the group can add them by hand. start_at/end_at are stored in UTC; attendees
-- is a human-readable comma-joined list (v1 does not resolve WhatsApp contacts to
-- calendar invitees). created_by records the participant who proposed it.
CREATE TABLE chat.whatsapp_calendar_draft (
    id           BIGSERIAL PRIMARY KEY,
    group_jid    TEXT NOT NULL,
    title        TEXT NOT NULL,
    start_at     TIMESTAMPTZ NOT NULL,
    end_at       TIMESTAMPTZ,
    attendees    TEXT,
    created_by   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    confirmed_at TIMESTAMPTZ
);

CREATE INDEX whatsapp_calendar_draft_group_open_idx
    ON chat.whatsapp_calendar_draft (group_jid, confirmed_at);
