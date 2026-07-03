-- chat.whatsapp_outbox: the single send path for all monolith-originated
-- WhatsApp traffic (ADR 039, spec section 3), mirroring chat.discord_outbox.
--
-- The WhatsApp gateway is a single-replica transport service (whatsmeow allows
-- one live socket per device session), so the monolith cannot send directly:
-- any monolith replica inserts a row here and the gateway drains it oldest-first
-- per group, sends via whatsmeow, and stamps posted_at + sent_message_id. This
-- is the same "compute writes a row, the connection-holder consumes it" pattern
-- as chat.discord_outbox, applied to WhatsApp verbs.
--
-- kind is the verb:
--   message  - send text to group_jid (optionally a reply via quoted_message_id).
--   edit     - edit a previous send (edit_of -> that row's sent_message_id) with
--              new content. WhatsApp only allows edits for ~15 minutes; when the
--              window has closed the gateway consumes the row with
--              last_error='edit_window_expired' and the monolith reposts a fresh
--              message rather than retrying forever.
--   reaction - react to target_message_id. whatsmeow's BuildReaction needs the
--              JID of the sender of the target message, which the row carries in
--              target_sender_jid (a spec extension over section 3: the outbox
--              must supply it because the gateway holds no message history to
--              look it up). reaction_remove sends an empty reaction to clear it.
--
-- sent_message_id is stamped by the gateway after a message send so later edits
-- and reactions can reference it.

CREATE TABLE chat.whatsapp_outbox (
    id                 BIGSERIAL PRIMARY KEY,
    group_jid          TEXT NOT NULL,
    kind               TEXT NOT NULL CHECK (kind IN ('message', 'edit', 'reaction')),
    content            TEXT,
    quoted_message_id  TEXT,
    edit_of            BIGINT,
    target_message_id  TEXT,
    -- JID of the sender of target_message_id, required to build a reaction.
    target_sender_jid  TEXT,
    reaction           TEXT,
    reaction_remove    BOOLEAN NOT NULL DEFAULT false,
    sent_message_id    TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    posted_at          TIMESTAMPTZ,
    attempts           INTEGER NOT NULL DEFAULT 0,
    last_error         TEXT,
    -- One combined per-kind shape check (the DiscordOutbox precedent keeps the
    -- kind validity in a single named constraint that the SQLModel fixtures
    -- mirror): message/edit need content; edit needs edit_of; a reaction needs
    -- target_message_id, target_sender_jid, and reaction all present.
    CONSTRAINT whatsapp_outbox_kind_valid CHECK (
        (kind = 'message' AND content IS NOT NULL)
        OR (kind = 'edit' AND content IS NOT NULL AND edit_of IS NOT NULL)
        OR (kind = 'reaction' AND target_message_id IS NOT NULL
            AND target_sender_jid IS NOT NULL AND reaction IS NOT NULL)
    )
);

-- The gateway drains oldest-first per group; a partial index over the unposted
-- rows keeps that scan cheap as posted rows accumulate. The (group_jid,
-- created_at, id) order matches the drain's ORDER BY so a group's sends leave in
-- insertion order (id breaks created_at ties).
CREATE INDEX whatsapp_outbox_pending
    ON chat.whatsapp_outbox (group_jid, created_at, id)
    WHERE posted_at IS NULL;
