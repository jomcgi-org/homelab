-- chat.whatsapp_pending_action: transient per-group conversational state for the
-- household capabilities (ADR 039 spec section 5). One pending action per group
-- (group_jid PK): a record intent awaiting an affirmative confirmation
-- (confirm-then-capture, the knowledge-graph consent boundary), or a calendar or
-- reminder intent awaiting a single clarifying answer (clarify-once). The next
-- engaged message in the group resolves or abandons it, so rows are short-lived.
-- summary holds the record confirmation text; payload carries the original intent
-- text (JSONB) so a clarifying follow-up can be combined with it. created_by
-- records which participant opened the pending action.
CREATE TABLE chat.whatsapp_pending_action (
    group_jid   TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    summary     TEXT,
    payload     JSONB,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT whatsapp_pending_action_kind_valid
        CHECK (kind IN ('record', 'calendar', 'reminder'))
);
