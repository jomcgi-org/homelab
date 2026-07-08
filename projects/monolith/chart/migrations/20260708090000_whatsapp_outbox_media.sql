-- WhatsApp media sends (ADR 039, amended): a `media` outbox kind so an image
-- (a chart PNG, an artifact preview) reaches the group inline instead of only as
-- text. The gateway uploads media_bytes to WhatsApp and sends an ImageMessage
-- with content as the optional caption. Bytes live in the row (chart PNGs are
-- small); no blob store round-trip in the gateway.

ALTER TABLE chat.whatsapp_outbox ADD COLUMN media_bytes bytea;
ALTER TABLE chat.whatsapp_outbox ADD COLUMN media_mime text;

-- Allow the new kind.
ALTER TABLE chat.whatsapp_outbox DROP CONSTRAINT whatsapp_outbox_kind_check;
ALTER TABLE chat.whatsapp_outbox ADD CONSTRAINT whatsapp_outbox_kind_check
    CHECK (kind = ANY (ARRAY['message'::text, 'edit'::text, 'reaction'::text, 'media'::text]));

-- Extend the per-kind shape guard: a media row needs the bytes and the mime;
-- content (the caption) stays optional.
ALTER TABLE chat.whatsapp_outbox DROP CONSTRAINT whatsapp_outbox_kind_valid;
ALTER TABLE chat.whatsapp_outbox ADD CONSTRAINT whatsapp_outbox_kind_valid
    CHECK (
        (kind = 'message' AND content IS NOT NULL)
        OR (kind = 'edit' AND content IS NOT NULL AND edit_of IS NOT NULL)
        OR (kind = 'reaction' AND target_message_id IS NOT NULL
            AND target_sender_jid IS NOT NULL AND reaction IS NOT NULL)
        OR (kind = 'media' AND media_bytes IS NOT NULL AND media_mime IS NOT NULL)
    );
