-- chat_public.messages.touched: persist each assistant turn's grounding (the
-- public notes it touched) so a shared snapshot can render the same GROUNDED IN
-- chips the live notes app shows.
--
-- Shape is a [{id, title}, ...] JSONB array, matching the node_touched events
-- and the response_cache.touched list. Empty for user turns. Adding a column
-- with a constant default is a metadata-only change in Postgres (no table
-- rewrite), so it is cheap on the hot messages table. Existing rows (sessions
-- that predate this) keep an empty array, so only turns recorded after the
-- deploy carry grounding into a snapshot, which is the expected behaviour.
--
-- No new grants: messages already grants INSERT/SELECT to public_writer and
-- SELECT to public_reader (20260617030000_chat_public.sql), and a new column is
-- covered by the existing table-level grant.

ALTER TABLE chat_public.messages
    ADD COLUMN touched JSONB NOT NULL DEFAULT '[]'::jsonb;
