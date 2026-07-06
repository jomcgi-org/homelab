-- Structural adventure layer (campaign side, NOT the entity spine).
-- An adventure is a contiguous seq range of a book's chunks; entity
-- membership is derived by join, never re-extracted.
CREATE TABLE grimoire.adventure (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    book_id text NOT NULL REFERENCES grimoire.book (id) ON DELETE CASCADE,
    name text NOT NULL,
    seq integer NOT NULL,
    summary text,
    level_range text,
    start_seq integer NOT NULL,
    end_seq integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT adventure_book_id_name_key UNIQUE (book_id, name),
    CONSTRAINT adventure_book_id_seq_key UNIQUE (book_id, seq),
    CONSTRAINT adventure_seq_range_chk CHECK (end_seq IS NULL OR end_seq >= start_seq)
);

CREATE INDEX adventure_book_id_idx ON grimoire.adventure (book_id);

-- Live entity roster per adventure. A view (not a link table) so it stays
-- fresh while the extraction drain keeps adding mentions.
CREATE VIEW grimoire.adventure_entity AS
SELECT DISTINCT a.id AS adventure_id, m.entity_id
FROM grimoire.adventure a
JOIN grimoire.knowledge_chunk kc
    ON kc.book_id = a.book_id
    AND kc.seq >= a.start_seq
    AND (a.end_seq IS NULL OR kc.seq <= a.end_seq)
JOIN grimoire.chunk_entity_mention m ON m.chunk_id = kc.id;

-- Same opt-in posture as 20260704000000_grimoire_public_reader_grant.sql:
-- named tables only, no schema-wide default privilege. The adventure layer
-- is structural corpus metadata (not campaign-private), so it joins the
-- public corpus grant.
GRANT SELECT ON
    grimoire.adventure,
    grimoire.adventure_entity
    TO public_reader;
