-- Index chat.whatsapp_outbox on sent_message_id so inbound directedness can look
-- up "is this quoted id one the bot sent" cheaply (ADR 039, reply-to-bot signal).
--
-- The gateway stamps sent_message_id with the real WhatsApp id it sent the row
-- under. A real WhatsApp reply to a bot message quotes that real id, which never
-- lands in the messages table (bot replies there use a synthetic wa-bot: id), so
-- the inbound handler resolves the quoted id against this column to decide the
-- reply is directed at the bot. Without the index that lookup is a full scan of
-- the outbox on every inbound message.
CREATE INDEX whatsapp_outbox_sent_message_id_idx
    ON chat.whatsapp_outbox (sent_message_id)
    WHERE sent_message_id IS NOT NULL;
