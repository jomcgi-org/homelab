# Grimoire Adventure Layer Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or subagent-driven-development) to implement this plan task-by-task.

**Goal:** Add a structural `adventure` layer between `book` and `chapter/content` so a DM can list the adventures in a book and see each adventure's roster of entities, without re-running entity extraction.

**Architecture:** Adventure boundary detection is a one-time judgment call over a fixed input (each book's section outline, already in Postgres) producing a small static output (~60 rows). So it is done by Claude in-session, not by a deployed LLM job: Claude reads each adventure book's outline (distinct level-2 `section_hierarchy` breadcrumbs ordered by `seq`) and classifies it into adventures using world knowledge of published D&D books. The output lands as a reviewable SEED MIGRATION of INSERTs. Boundaries are stored as contiguous `seq` ranges (`start_seq`..`end_seq`); entity membership is a pure live join: `adventure -> knowledge_chunk (seq in range) -> chunk_entity_mention -> entity`. Entity extraction is NEVER re-run. A repo Claude skill documents the classification procedure so future book uploads can be classified the same way.

**Tech Stack:** Postgres (Atlas migrations, incl. a data seed migration), SQLModel, FastAPI routers, SvelteKit public pages, a repo Claude skill. No new job, no cronworkflow, no LLM client changes.

**Repo rules that bind every task:** no em-dashes anywhere; Conventional Commits; no local test runs (tests are written, hand-registered in `projects/monolith/BUILD` as `py_test`, and executed by CI after the final push); chart bump in the same PR because migrations deploy.

---

## Context (verified against live DB and code)

- `grimoire.knowledge_chunk(book_id, chunk_ref, content, section_path, section_hierarchy, seq, image_ref)`. `section_hierarchy` is a ` > `-joined breadcrumb whose level-1 segment is the book title, e.g. `CANDLEKEEP MYSTERIES™ > THE JOY OF EXTRADIMENSIONAL SPACES > AREA 4`. ~98% coverage. `seq` is reading order.
- Level-2 segments are CLEAN for anthologies (Candlekeep's 17 adventure titles appear as level-2 roots in `seq` order) and NOISY for some single-adventure books (Curse of Strahd has ~250 pseudo-roots because deep headings sometimes surface at level 2). This is why boundaries are resolved to `seq` ranges, never matched by breadcrumb strings.
- `book_kind` is code-side only: `BOOK_KIND` dict + `_BOOK_KIND_PREFIXES` in `projects/monolith/grimoire/extract.py:402-442`. There is no `book_kind` column. PR #3241 (extract v5 prompt) does NOT touch it (verified against its diff); it edits a different region of `extract.py`, so rebase risk is low.
- Latest migration: `projects/monolith/chart/migrations/20260705150000_grimoire_extraction_v4.sql`. New migrations must sort after it.
- Tests: SQLite + `SQLModel.metadata.create_all` (never migrations), hand-registered `py_test` targets in `projects/monolith/BUILD` (grimoire is gazelle-excluded).
- Public tier: `grimoire/router_public.py` + `grimoire/library.py` serve the public book reader. New public-readable tables need `GRANT SELECT` to the public reader role in the migration (check the grant pattern at the bottom of `20260703070000_grimoire_schema.sql` and mirror it exactly, including the role name).
- DB access for outline pulls: `kubectl exec -n monolith monolith-pg-1 -c postgres -- psql -U postgres -d monolith` (reads only; all writes go through migrations).
- In-scope books = `book_kind() in {"adventure", "adventure-anthology"}` after Task 3:
  - Anthologies: candlekeep-mysteries (17 adventures), tales-from-the-yawning-portal (7), keys-from-the-golden-vault (13), ghosts-of-saltmarsh (7).
  - Single-adventure: curse-of-strahd (1; Death House is a prelude INSIDE the one adventure), lost-mine-of-phandelver, storm-kings-thunder, waterdeep-dragon-heist, waterdeep-dungeon-of-the-mad-mage, tomb-of-annihilation, descent-into-avernus, rime-of-the-frostmaiden, the-wild-beyond-the-witchlight, planescape-adventures-in-the-multiverse (slipcase; contains Turn of Fortune's Wheel; classify from the outline, may legitimately yield 1).
- `waterdeep-dungeon-of-the-mad-mage` has only ~146 chunks loaded (drain in progress). It is single-adventure, so seed it with `start_seq` at the first content chunk and `end_seq NULL` ("to end of book"): the live view auto-absorbs chunks as the drain lands them.

---

### Task 1: Migration: `grimoire.adventure` table + entity join view

**Files:**
- Create: `projects/monolith/chart/migrations/20260705160000_grimoire_adventure.sql`

**Step 1: Read the grant + constraint conventions**

Read the tail of `20260703070000_grimoire_schema.sql` (grants) and the CHECK/UNIQUE naming in `20260705150000_grimoire_extraction_v4.sql` (`table_column_chk` / `table_columns_key`).

**Step 2: Write the migration**

```sql
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
```

Then add the SELECT grants for the public reader role, mirroring the existing grant block verbatim for both `grimoire.adventure` and `grimoire.adventure_entity` (repo memory: a missing public_reader grant surfaces as public-tier 503s).

`seq` = display ordinal within the book (1..N). `start_seq`/`end_seq` = chunk `seq` range. `end_seq IS NULL` means "to end of book".

**Step 3: Sanity-check ordering + atlas.sum**

Confirm the filename sorts after `20260705150000_...`. If the migrations dir has an `atlas.sum`, regenerate it with the CI-pinned community atlas v1.1.0 (repo memory) after BOTH migration files exist (this task and Task 4), in one go.

**Step 4: Commit**

```bash
git add projects/monolith/chart/migrations/
git commit -m "feat(grimoire): adventure table + entity join view migration"
```

---

### Task 2: `Adventure` SQLModel

**Files:**
- Modify: `projects/monolith/grimoire/models.py` (add after `Book`, ~line 296)
- Modify: `projects/monolith/grimoire/models_test.py`

**Step 1: Write failing tests** in `models_test.py`, following the file's existing fixture style (SQLite `create_all`): create a Book, two KnowledgeChunks with seqs, an Adventure with a range; assert round-trip and that `(book_id, name)` duplicates raise IntegrityError.

**Step 2: Implement the model**, copying the field conventions of `Book`/`KnowledgeChunk` exactly (schema qualification, created_at default, FK style):

```python
class Adventure(SQLModel, table=True):
    """One self-contained runnable module within a book (structural layer).

    Boundaries are a contiguous ``seq`` range over the book's chunks;
    entity membership is derived by join (see grimoire.adventure_entity),
    never re-extracted.
    """

    __tablename__ = "adventure"
    __table_args__ = (
        UniqueConstraint("book_id", "name", name="adventure_book_id_name_key"),
        UniqueConstraint("book_id", "seq", name="adventure_book_id_seq_key"),
        {"schema": "grimoire"},
    )

    id: uuid.UUID | None = Field(default_factory=uuid.uuid4, primary_key=True)
    book_id: str = Field(foreign_key="grimoire.book.id", index=True)
    name: str
    seq: int
    summary: str | None = None
    level_range: str | None = None
    start_seq: int
    end_seq: int | None = None
    # created_at: copy Book's pattern verbatim
```

**Step 3: Commit** `feat(grimoire): Adventure SQLModel`

---

### Task 3: `BOOK_KIND`: add anthologies and missing adventures

**Files:**
- Modify: `projects/monolith/grimoire/extract.py:402-427`
- Modify: `projects/monolith/grimoire/extract_test.py` (book_kind coverage)

**Step 1:** Extend `BOOK_KIND` (keep existing entries untouched):

```python
    "candlekeep-mysteries": "adventure-anthology",
    "tales-from-the-yawning-portal": "adventure-anthology",
    "keys-from-the-golden-vault": "adventure-anthology",
    "ghosts-of-saltmarsh": "adventure-anthology",
    "tomb-of-annihilation": "adventure",
    "descent-into-avernus": "adventure",
    "the-wild-beyond-the-witchlight": "adventure",
    "waterdeep-dungeon-of-the-mad-mage": "adventure",
```

Add next to `book_kind()`:

```python
ADVENTURE_BOOK_KINDS = frozenset({"adventure", "adventure-anthology"})
```

Commit-body note: `adventure-anthology` also flows into the extraction user message for FUTURE chunks of these books (previously title-only context), which is strictly more context; no markers are invalidated and nothing is re-extracted.

**Step 2: Test** `book_kind("candlekeep-mysteries") == "adventure-anthology"`, unmapped slug still `None`.

**Step 3: Commit** `feat(grimoire): book_kind anthology split + missing adventure mappings`

---

### Task 4: In-session classification -> seed migration (Claude does the judgment)

This replaces the deployed-LLM job of the earlier design. The classifier is Claude Code in this session; the deliverable is a data migration.

**Files:**
- Create: `projects/monolith/chart/migrations/20260705170000_grimoire_adventure_seed.sql`

**Step 1: Pull each in-scope book's outline** (read-only psql). Per book:

```bash
kubectl exec -n monolith monolith-pg-1 -c postgres -- psql -U postgres -d monolith -Atc "
select root, count(*) as chunks, min(seq) as first_seq
from (select split_part(section_hierarchy,' > ',2) as root, seq
      from grimoire.knowledge_chunk
      where book_id='<BOOK_ID>' and section_hierarchy is not null) t
group by root order by min(seq);"
```

Also grab `max(seq)` per book.

**Step 2: Classify.** For each book, decide the adventures using the outline + world knowledge:
- Single-adventure books: exactly one adventure, `start_seq` = first content chunk (skip credits/contents front matter), `end_seq` = NULL. Name = the adventure's published name, level_range from the published book (e.g. Lost Mine `1-5`, Curse of Strahd `1-10`), 1-2 sentence summary.
- Anthologies: one row per adventure, `start_seq` = the outline `first_seq` of the adventure's title section; adventure i's `end_seq` = adventure i+1's `start_seq - 1`; the last adventure ends just before trailing shared appendices (e.g. Candlekeep's CONTRIBUTOR BIOS) or at `max(seq)` if none.
- Validate counts: candlekeep-mysteries=17, tales-from-the-yawning-portal=7, keys-from-the-golden-vault=13, ghosts-of-saltmarsh=7, and the single-adventure books=1 each. Investigate any mismatch against the outline before writing SQL (a missing adventure title in the outline usually means noisy hierarchy; fall back to the level-2 rows around the expected seq position, or check level 3).

**Step 3: Emit the seed migration.** Idempotent upsert keyed on the natural key so a re-run (or a later re-seed migration) is safe:

```sql
-- Adventure seed: boundaries classified from each book's section outline
-- (Claude Code in-session, 2026-07-05). start_seq/end_seq are chunk seq
-- ranges; end_seq NULL = to end of book. Derived data: safe to re-seed.
INSERT INTO grimoire.adventure (book_id, name, seq, summary, level_range, start_seq, end_seq)
VALUES
    ('candlekeep-mysteries', 'The Joy of Extradimensional Spaces', 1, '...', '1', 71, 157),
    -- ... every adventure for every in-scope book ...
ON CONFLICT (book_id, name) DO UPDATE SET
    seq = EXCLUDED.seq,
    summary = EXCLUDED.summary,
    level_range = EXCLUDED.level_range,
    start_seq = EXCLUDED.start_seq,
    end_seq = EXCLUDED.end_seq;
```

Size check: ~60 rows of short text is a few KB, nowhere near the 256KiB migrations-ConfigMap cap.

**Step 4: Spot-check the ranges read-only in psql** before committing, e.g. entities for one Candlekeep adventure via the would-be join (run the view's SELECT with literal seq bounds); expect Fistandia-adjacent NPCs for The Joy of Extradimensional Spaces, and no bleed from Book of the Raven.

**Step 5: Regenerate `atlas.sum` if present** (covers Task 1's file too), then commit:

```bash
git add projects/monolith/chart/migrations/
git commit -m "feat(grimoire): seed adventures for all adventure books"
```

---

### Task 5: Claude skill: classify a future book's hierarchy

**Files:**
- Create: `.claude/skills/grimoire-classify-adventures/SKILL.md`

A repo skill capturing the procedure so the next book upload gets the same treatment. Content (concise, follows any existing repo-skill conventions; check `.claude/skills/` for an existing example to mirror):

- **When to use:** after a new adventure/anthology book is uploaded and its chunks + section_hierarchy are loaded (and it is mapped in `BOOK_KIND`).
- **Inputs:** `book_id`.
- **Procedure:** (1) pull the outline with the Task 4 psql query + `max(seq)`; (2) classify into adventures using world knowledge (rules from Task 4 Step 2 inlined: single vs anthology, front-matter/appendix exclusion, seq-range resolution, end_seq NULL semantics); (3) append a new upsert seed migration (`ON CONFLICT (book_id, name) DO UPDATE`, new timestamp after the current head) plus `atlas.sum` regen if present; (4) spot-check the roster join read-only; (5) worktree + PR + chart bump per repo rules.
- **Invariants:** never re-run entity extraction; never write to the DB directly; adventures are structure, not entity_type taxonomy.

**Commit** `docs(grimoire): skill for classifying new books into adventures`

---

### Task 6: API: adventures per book, entities per adventure

**Files:**
- Modify: `projects/monolith/grimoire/library.py` (query helpers)
- Modify: `projects/monolith/grimoire/router_public.py`
- Modify: `projects/monolith/grimoire/library_test.py` and/or `router_public_test.py`

**Step 1: Helpers** in `library.py` (pure SQLModel queries so SQLite tests work; do NOT query the Postgres view from app code):

- `list_adventures(session, book_id) -> list[dict]`: adventures ordered by `seq`, each with `entity_count` (join chunks+mentions over the seq range, COUNT DISTINCT entity).
- `adventure_entities(session, adventure_id) -> dict`: the adventure row + its DISTINCT entities (id, name, entity_type, category) via `adventure -> knowledge_chunk(seq range) -> chunk_entity_mention -> entity`.

**Step 2: Routes** in `router_public.py`, matching the existing endpoint style/response envelopes:

- `GET /api/grimoire/books/{book_id}/adventures`
- `GET /api/grimoire/adventures/{adventure_id}` (detail + entity roster grouped by entity_type)

**Step 3: Tests**: seed SQLite with a 2-adventure book, chunks, mentions, entities; assert the roster respects seq boundaries, `end_seq NULL` extends to end of book, and an entity mentioned in two chunks of one adventure appears once.

Watch for `bdd_completeness_test` if new public callables trip it (repo memory); add the BDD stubs it demands in the same commit.

**Step 4: Commit** `feat(grimoire): adventure list + roster public API`

---

### Task 7: Frontend: surface adventures in the public book reader

**Files:**
- Modify: the public book page (find it by following how `/grimoire/books/{book_id}` fetches `sections`)
- Create: adventure detail page `/grimoire/adventures/[id]` following the neighbors' conventions

**Step 1:** On the book page, when `GET .../adventures` returns rows, render an "Adventures" section above the section tree: name, level range, summary, entity count, linking to the detail page. Books with no rows render unchanged.

**Step 2:** Detail page: adventure header (name, level range, summary, parent book link) + roster grouped by entity_type, each entity linking to the existing public entity page. Reuse the neighbors' load pattern; add no npm deps.

**Step 3: Commit** `feat(grimoire): adventures on public book page + roster page`

---

### Task 8: Chart bump, push, CI, merge

1. `bazel/tools/format/fast-format.sh` (repo-relative path; bare `format` is not on PATH in worktree shells), commit any fallout.
2. `bazel/tools/git/bump-chart.sh projects/monolith`, commit `build(monolith): bump chart for grimoire adventure layer`.
3. Push branch, open PR (body: containment model, "no re-extraction" invariant, the validation table of expected adventure counts per book). No em-dashes.
4. `gh pr checks <n> --watch`; on failure read logs via `mcp__buildbuddy__get_invocation` (commitSha selector) and quote the actual error before hypothesizing.
5. Merge with `gh pr merge --rebase` (rebase-only repo).

### Task 9: Post-merge validation

1. Verify ArgoCD sync + Atlas applied both migrations (argocd-outofsync runbook if stuck).
2. psql: `select book_id, count(*) from grimoire.adventure group by 1 order by 1;` matches the expected-count table; spot-check `adventure_entity` for The Joy of Extradimensional Spaces and confirm Curse of Strahd = 1 row spanning the book.
3. Confirm the public pages render: `https://jomcgi.dev/grimoire/books/candlekeep-mysteries` shows 17 adventures; an adventure page shows a grouped roster.
