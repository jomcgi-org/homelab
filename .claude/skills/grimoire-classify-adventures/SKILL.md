---
name: grimoire-classify-adventures
description: Classify a newly loaded Grimoire adventure book's section hierarchy into structural adventure rows (seed migration). Use after a new adventure or anthology book's chunks and section_hierarchy are loaded, when the user asks to "classify adventures" for a book, or when a book's adventures are missing from grimoire.adventure.
---

# Grimoire: classify a book into adventures

Turn a loaded book's section outline into rows in `grimoire.adventure`, delivered as a seed migration. The classifier is you (Claude), using the outline plus world knowledge of the published book. This is structural derivation only.

**Invariants (never violate):**

- NEVER re-run entity extraction or re-embed anything. Entity membership is a live join (`grimoire.adventure_entity` view over chunk seq ranges); it needs no per-entity work.
- NEVER write to the database directly. Output is a migration file in `projects/monolith/chart/migrations/`, applied by Atlas via GitOps.
- Adventures are structure, not content: do not add `adventure` to the `entity_type` taxonomy.
- Scope: only books whose `book_kind()` (in `projects/monolith/grimoire/extract.py`) is `adventure` or `adventure-anthology`. Bestiaries, rulebooks, and setting guides get no rows. If the new book is unmapped, add it to `BOOK_KIND` first.

## Procedure

1. **Pull the outline** (read-only):

   ```bash
   kubectl exec -n monolith monolith-pg-1 -c postgres -- psql -U postgres -d monolith -Atc "
   select root, count(*) as chunks, min(seq) as first_seq
   from (select split_part(section_hierarchy,' > ',2) as root, seq
         from grimoire.knowledge_chunk
         where book_id='<BOOK_ID>' and section_hierarchy is not null) t
   group by root order by min(seq);"
   ```

   Also grab `min(seq)`/`max(seq)` for the book. Confirm the book looks fully loaded (an outline that stops mid-book means the drain is incomplete; classify anyway only if the book is single-adventure, else wait).

2. **Classify.**
   - **Single-adventure book**: exactly one row. `start_seq` = the book's `min(seq)`, `end_seq` = NULL (whole book, including front matter and appendices; NULL absorbs chunks loaded later). Name = published adventure title, `level_range` from the published book, 1-2 sentence summary.
   - **Anthology**: one row per published adventure. Do NOT trust the level-2 outline alone (titles are sometimes nested oddly, and tables of contents produce false early matches). Locate each known adventure title anywhere in the breadcrumb, flooring the search above the book's front matter / overview chapters:

     ```sql
     select min(seq) from grimoire.knowledge_chunk
     where book_id='<BOOK_ID>' and seq >= <floor>
       and section_hierarchy ilike '%<DISTINCTIVE TITLE SUBSTRING>%';
     ```

     Verify the starts are strictly increasing and match the published adventure order. Adventure i ends at adventure i+1's start minus 1; the last adventure ends just before the trailing shared appendices (stat blocks, magic items, contributor bios). Peek at chunk `content` around any ambiguous boundary before deciding.

3. **Emit a seed migration**: new file `projects/monolith/chart/migrations/<next-timestamp>_grimoire_adventure_seed_<book_id>.sql`, timestamped after the current head migration. Upsert on the natural key so re-runs and corrections are safe:

   ```sql
   INSERT INTO grimoire.adventure (book_id, name, seq, summary, level_range, start_seq, end_seq)
   VALUES (...)
   ON CONFLICT (book_id, name) DO UPDATE SET
       seq = EXCLUDED.seq, summary = EXCLUDED.summary,
       level_range = EXCLUDED.level_range,
       start_seq = EXCLUDED.start_seq, end_seq = EXCLUDED.end_seq;
   ```

   `seq` is the display ordinal (1..N within the book). The pre-commit hook refreshes Atlas checksums when migrations change.

   The seed MUST also upsert the parent book row before the adventure INSERT (`INSERT INTO grimoire.book (id, display_name) VALUES (...) ON CONFLICT (id) DO NOTHING;`): prod already has it, but the CI test harness applies all migrations to an empty database and the adventure `book_id` FK fails without it.

4. **Spot-check read-only** before committing: run the `grimoire.adventure_entity` view's join with literal bounds for one adventure and sanity-check the roster (no bleed from the neighboring adventure). If the book has no `chunk_entity_mention` rows yet (extraction drain pending), skip this; the view fills in later.

5. **Ship it**: worktree + PR per repo rules, Conventional Commit (`feat(grimoire): seed adventures for <book_id>`), chart bump in the same PR (`bazel/tools/git/bump-chart.sh projects/monolith`), merge on green CI.

## Reference example

`20260705170000_grimoire_adventure_seed.sql` seeded the initial 13 books. Boundary decisions to mirror when classifying new books (folded from the original method notes): watch for ToC false matches (Tales from the Yawning Portal), an overview chapter contaminating title matches (Adventures in Saltmarsh), and front matter that ends mid-book (Candlekeep front matter ends at seq 70). Boundaries are always resolved to contiguous `seq` ranges, never matched by breadcrumb strings, because level-2 segments are noisy for some single-adventure books.

Known pending case: `planescape-adventures-in-the-multiverse` has only its setting-guide volume loaded (chunks end at Chapter 3: The Outlands). When the Turn of Fortune's Wheel volume lands, classify it via this skill.
