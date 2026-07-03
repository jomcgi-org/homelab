-- image_ref: full s3:// URI of the source illustration for image-derived
-- chunks (Marker Picture blocks), NULL for text chunks. Stored so the app can
-- later render the picture (via imgproxy) alongside the retrieved chunk; the
-- caption text still flows through embedding + entity extraction like any
-- other chunk. Mirror of KnowledgeChunk.image_ref in grimoire/models.py.
ALTER TABLE grimoire.knowledge_chunk ADD COLUMN image_ref TEXT;
