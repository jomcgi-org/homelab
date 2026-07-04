-- Follow-up to 20260704000000: grant public_reader SELECT on the two corpus
-- metadata tables the public Library read path also needs. list_books reads
-- grimoire.book (display names) and grimoire.chunk_extraction (the extraction
-- markers behind the coverage count), and search_public reads grimoire.book for
-- lore-hit display names. Both are corpus metadata, not campaign-private data,
-- so they join the public corpus grant. Same opt-in posture as the first
-- migration: named tables only, no schema-wide default privilege.

GRANT SELECT ON
    grimoire.book,
    grimoire.chunk_extraction
    TO public_reader;
