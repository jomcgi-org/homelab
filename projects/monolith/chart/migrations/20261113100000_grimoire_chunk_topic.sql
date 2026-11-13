-- Staged metadata only. Existing corpus classification is an explicit paged operation.
ALTER TABLE grimoire.knowledge_chunk
    ADD COLUMN topic text,
    ADD CONSTRAINT knowledge_chunk_topic_chk
        CHECK (topic IS NULL OR topic = 'dm_guidance');
