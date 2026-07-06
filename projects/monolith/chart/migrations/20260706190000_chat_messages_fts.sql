-- Hybrid history search: add a full-text search vector alongside the existing
-- pgvector embedding so chat history recall can fuse semantic (vector) and
-- lexical (keyword) matches. Vector search alone misses exact tokens a user
-- asks for by name: a username, a URL, an error code, a rare proper noun.
--
-- content_tsv is a GENERATED STORED column so it is maintained automatically on
-- every insert/update with no application bookkeeping. The GIN index makes the
-- @@ / ts_rank lookups fast; queries stay channel-scoped via the existing
-- channel indexes.
ALTER TABLE chat.messages
    ADD COLUMN content_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;

CREATE INDEX chat_messages_content_tsv ON chat.messages USING gin (content_tsv);
