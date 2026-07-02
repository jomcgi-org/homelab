-- Allow reaction rows in the Discord outbox.
--
-- 20260702150000 added a reaction verb (target_message_id/reaction/reaction_remove)
-- so the goose runner can drive ⏳/👀/✅ on a message off-loop. A reaction row
-- carries neither content nor embed_json, which violates the original
-- discord_outbox_content_or_embed CHECK (content OR embed). That rejected every
-- mark_inflight_running / ack_inflight insert in prod, wedging any thread with
-- queued replies (the SQLite test fixtures build the table from the model, which
-- did not carry this CHECK, so the tests never saw it).
--
-- Relax the constraint to accept a reaction row as a valid third kind of outbox
-- entry, while still rejecting a fully-empty row.

ALTER TABLE chat.discord_outbox
    DROP CONSTRAINT IF EXISTS discord_outbox_content_or_embed;

ALTER TABLE chat.discord_outbox
    ADD CONSTRAINT discord_outbox_content_or_embed
        CHECK (content IS NOT NULL OR embed_json IS NOT NULL OR reaction IS NOT NULL);
