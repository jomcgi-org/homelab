-- Provider discriminator + WhatsApp session-keying columns for goosecracker
-- sessions (ADR 039 Phase 4). A goosecracker session is no longer Discord-only:
-- the WhatsApp household gateway runs group-keyed agent sessions through the
-- same machinery, discriminated by ``provider``. Discord rows keep the default.
--
-- The session PK (``discord_thread``) holds a sanitized ``wa-<group_jid>`` key
-- for a WhatsApp session (the raw JID contains ':', '@', '.', which the internal
-- progress/steering endpoints reject via their ^[A-Za-z0-9_-]{1,64}$ guard), so
-- the real group JID is stored explicitly in ``provider_group_jid`` for the
-- reverse lookup the outbox writers need. The PK is derived from the group, so
-- one row per group falls out naturally (one active session per group).
--
-- ``provider_trigger_message_id`` / ``provider_trigger_sender_jid`` carry the
-- triggering WhatsApp message and its sender JID so the reaction lifecycle
-- (⏳ → 👀 → ✅) can build reactions on that message (whatsmeow's reaction build
-- needs the target sender JID). ``checklist_outbox_id`` is the outbox id of the
-- live checklist message the run edits at stage boundaries; it is repointed to a
-- fresh message when WhatsApp's ~15-minute edit window closes mid-run.
--
-- Additive, all defaulted, safe on existing rows.
ALTER TABLE chat.goosecracker_sessions
    ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'discord',
    ADD COLUMN IF NOT EXISTS provider_group_jid TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS provider_trigger_message_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS provider_trigger_sender_jid TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS checklist_outbox_id BIGINT;

-- Author display name for steering attribution: WhatsApp carries a sender push
-- name alongside the JID, so a steering row records both (the JID in author_id,
-- the readable name here). Defaulted '' so existing Discord steering rows, which
-- attribute by Discord user id in author_id, are unchanged.
ALTER TABLE chat.goosecracker_steering
    ADD COLUMN IF NOT EXISTS author_name TEXT NOT NULL DEFAULT '';
