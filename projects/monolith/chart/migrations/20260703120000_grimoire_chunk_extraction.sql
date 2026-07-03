-- Processed-marker table for grimoire entity extraction, per
-- docs/plans/2026-07-03-grimoire-extract-qwen-cachekey.md Task 1. A row means
-- "this chunk is done under this exact (model, prompt_hash)"; absence means
-- pending. status='empty' marks zero-yield chunks so they are not re-run
-- forever; failures write no row so they are naturally re-selected.
CREATE TABLE grimoire.chunk_extraction (
    chunk_id     UUID NOT NULL REFERENCES grimoire.knowledge_chunk (id) ON DELETE CASCADE,
    model        TEXT NOT NULL,
    prompt_hash  TEXT NOT NULL,
    status       TEXT NOT NULL,
    extracted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chunk_id, model, prompt_hash),
    CONSTRAINT chunk_extraction_status_chk CHECK (status IN ('ok', 'empty'))
);
