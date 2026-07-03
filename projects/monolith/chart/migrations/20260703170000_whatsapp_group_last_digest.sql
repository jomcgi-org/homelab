-- chat.whatsapp_group.last_digest_at: the timestamp of the last morning digest
-- sent to the group (ADR 039 spec section 5d). The hourly digest job dedupes on
-- this (at most one digest per local day) while honouring the group's quiet hours,
-- so a group whose configured send time falls inside quiet hours gets the digest
-- at the first waking hour past the send time rather than a duplicate or a miss.
ALTER TABLE chat.whatsapp_group
    ADD COLUMN last_digest_at TIMESTAMPTZ;
