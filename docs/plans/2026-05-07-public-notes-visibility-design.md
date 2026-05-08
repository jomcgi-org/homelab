# Public Notes — Visibility Labels & Public Surface

**Date:** 2026-05-07
**Status:** Design approved; implementation plan to follow.
**Author:** Joe + Claude (brainstorming session)

## Goal

Add a `visibility: public | private` label to every note so a strict, opt-in
subset of the knowledge graph can be exposed on the public website without
leaking PII about Joe, his colleagues, his job search, or his personal life.

The vault stays canonical. The DB mirrors visibility for cheap filtered
queries. The labelling is LLM-driven from a shared, precise criteria block.
Defaults are conservative: anything unlabelled is treated as private.

## Phasing

- **V1 — Backend wiring (this design).**
  Schema + frontmatter + reconciler + prompt updates + public API + two
  vault-mutation scripts (mechanical add, then LLM backfill). No frontend
  changes; the existing private notes page is untouched.
- **V2 — Public notes page (separate plan).** SvelteKit page under
  `routes/public/notes/` consuming the V1 public API.
- **V3 — Public chat (separate plan).** KG-animated chat backed by qwen,
  with strict rate limits on the public surface.

## Locked-in decisions

| Decision                              | Choice                                                                         | Rationale                                                                                                          |
| ------------------------------------- | ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------ |
| Source of truth                       | Frontmatter canonical, DB column mirrors                                       | Vault stays self-describing; SQL filtering stays cheap; same shape as `status`.                                    |
| State space                           | Binary `public \| private`, null = private                                     | Keeps prompts and routes simple. `needs_review` deferred unless classifier confidence proves to be a real problem. |
| Public render of `[[private-target]]` | Strip wikilink to plain text; drop the edge from the public graph              | Renderer-side stripping; bracket text leak accepted (low likelihood of acute PII inside link names).               |
| Migration                             | Phase 1 mechanical-add (idempotent), then Phase 2 LLM backfill (single commit) | Phase 1 unblocks all backend code work; Phase 2 yields a single revertable vault diff.                             |
| Backfill report                       | Written outside the working tree (e.g. `/tmp/`)                                | Rationales quote note content; never committed.                                                                    |
| Public API namespace                  | `/api/knowledge/public/...`                                                    | Cache + auth divergence is obvious from the URL.                                                                   |

## Data model

### Migration

`chart/migrations/YYYYMMDDHHMMSS_knowledge_notes_visibility.sql`:

```sql
ALTER TABLE knowledge.notes
  ADD COLUMN visibility text NULL;

ALTER TABLE knowledge.notes
  ADD CONSTRAINT notes_visibility_chk
  CHECK (visibility IS NULL OR visibility IN ('public', 'private'));

CREATE INDEX notes_visibility_idx
  ON knowledge.notes (visibility)
  WHERE visibility = 'public';
```

Partial index because the public set is the small, hot read path; private/null
reads do not need it.

### `Note` SQLModel (`projects/monolith/knowledge/models.py`)

```python
Visibility = Literal["public", "private"]

class Note(SQLModel, table=True):
    ...
    visibility: Visibility | None = Field(
        default=None, sa_column=Column(String, nullable=True)
    )
```

### Frontmatter parser (`projects/monolith/knowledge/frontmatter.py`)

- Add `"visibility"` to `_PROMOTED_KEYS`.
- Add `visibility: str | None = None` to `ParsedFrontmatter`.
- `meta.visibility = _str_or_none(data.get("visibility"))` in `_build`.
- Validate value is `public` / `private` / empty; **warn-and-treat-as-null** on
  anything else. Never fail the parse — bad classifier output must not break
  ingest.

### Reconciler / store

Wherever `status` is written from `ParsedFrontmatter` to `Note` in
`store.py`, write `visibility` the same way. Frontmatter-canonical means every
reconcile resyncs the DB from disk.

### Coalescing rule

Codified once in `knowledge/visibility.py`:

```python
def effective_visibility(note: Note) -> Literal["public", "private"]:
    return note.visibility or "private"
```

Never inlined elsewhere — flipping the default later is a one-line change.

## Visibility criteria (the load-bearing artifact)

Lives in a new module `projects/monolith/knowledge/visibility.py` and is
imported by every prompt that emits frontmatter and by the backfill script.
Single source of truth — drift is the most common way these things go wrong.

```python
VISIBILITY_CRITERIA = """\
## Visibility (REQUIRED frontmatter field)

Every note MUST set `visibility: public` or `visibility: private`.
This controls whether the note appears on Joe's public website.

Default to `private` whenever you are uncertain.

Mark `public` when the note is about:
- General engineering concepts, principles, heuristics (DORA, Conway's Law,
  blameless postmortems, etc.) — anything you'd find in a textbook, blog,
  or conference talk.
- Skills, technologies, or methods covered in Joe's public CV / GitHub /
  conference talks.
- Verifiable facts about external systems, libraries, protocols, or tools.
- Book / paper / talk summaries when the source is publicly available.

Mark `private` when the note involves any of:
- Names of current or former colleagues, managers, reports, or interviewers.
- Specific employers in non-public ways: project codenames, internal
  architecture, compensation, performance reviews, hiring decisions.
- Job-search activity: interview prep, comp negotiation, target companies,
  reasons-for-leaving, offer comparisons.
- Personal life: family, finances, health, relationships, legal matters,
  living situation.
- Critiques or hot takes about identifiable people or companies that
  aren't already in Joe's public writing.
- Active tasks, daily/weekly journals, blockers — anything operational
  about Joe's current work.

Edge cases:
- An atom about a generally-applicable pattern that includes a
  workplace-specific example: rewrite the example out and mark public,
  OR keep the example and mark private. Do not mark public with the
  example intact.
- A fact about an external library mentioned during a private incident:
  the fact is public, the incident framing is private — split into two
  notes if needed.

When in doubt: `private`.
"""
```

## Prompt updates

- **Gardener atom prompt** (`gardener.py` `_CLAUDE_PROMPT_HEADER`) — append
  `VISIBILITY_CRITERIA`; add `visibility: public|private` to the required
  frontmatter template, before `tags:`.
- **Gardener distill prompt** (`_DISTILL_PROMPT`) — same: append criteria, add
  `visibility:` to the required-fields list. Distilled task notes will
  usually steer to `private`.
- **Research writer** (`research_writer.py` `write_research_raw`) — set
  `"visibility": "public"` directly in the frontmatter dict for `type:
research` raws. The downstream gardener still re-classifies during
  decomposition; this is a hint, not a guarantee.
- **Manual edit endpoint** (`router.py` `PUT /api/knowledge/notes/{id}`) — no
  code change; frontmatter passes through. Add one round-trip test to
  guarantee the field survives.

**Not changed:** the gap-classifier prompt (`gap_classifier.py`). It emits
`external | internal | hybrid | parked` for _gaps_, a different axis from
visibility. Conflating the two is the bug recorded in
`memory/project_kg_gap_drain.md`.

## Vault migration scripts

Both live under `projects/monolith/knowledge/tools/` as Bazel `py_binary`
targets. Both operate on the vault filesystem only — no DB writes; the
reconciler picks up the resulting frontmatter changes on its next pass.

### Phase 1 — `add_visibility_field` (mechanical, no LLM)

```
bazel run //projects/monolith/knowledge/tools:add_visibility_field -- \
  --vault-root /path/to/vault --dirs _processed
```

- Walks configured dirs (default `_processed`).
- For each `.md`: parse via `frontmatter.parse()`. If `visibility` already
  present, skip. Otherwise insert `visibility:` (empty) into the YAML block,
  preserving existing key order. Atomic temp-file + rename.
- Insertion uses an in-place line edit on the raw frontmatter block (not a
  full `yaml.dump` round-trip) so the diff stays small and auditable.
- Idempotent.
- Single commit on the vault repo when done.
- **Unblocks all backend code work** — schema, parser, public routes can
  land in parallel before Phase 2 runs.

### Phase 2 — `classify_visibility_backfill` (LLM, single commit)

```
bazel run //projects/monolith/knowledge/tools:classify_visibility_backfill -- \
  --vault-root /path/to/vault --report /tmp/visibility-report.json
```

- Walks `_processed/`, finds notes with null/empty `visibility`.
- One LLM call per note via the `claude` CLI subprocess (per
  `memory/feedback_claude_cli_subprocess_for_tos.md` — never the Anthropic
  SDK). Prompt = `VISIBILITY_CRITERIA` + the note's title + tags + body.
  Strict JSON-mode response: `{"visibility": "public"|"private",
"rationale": "..."}`.
- Parse failure / non-zero exit / timeout → leave the file unchanged, log
  the failure in the report, default to `private` only if the script is
  forced to set a value (it is not — the file just stays unlabelled and
  the next run picks it up).
- Internally batched with concurrency cap for crash recovery; **no per-batch
  commits**. All label changes accumulate on disk; Joe commits the entire
  labelled vault in one commit.
- Report path is required and must be **outside the repo working tree**
  (script enforces; refuses to write inside the repo). Recommended:
  `/tmp/visibility-report.json`.
- Resumable: re-running picks up only the still-unlabelled notes.

**Revert plan:** `git revert <backfill-sha>` on the vault repo rolls back
every label; tune criteria; re-run.

## Public API surface

Two new endpoints in `projects/monolith/knowledge/router.py`, mounted under
`/api/knowledge/public/...`. Separate namespace (vs query-param filtering on
existing routes) so:

- Cache headers diverge cleanly (public can cache much longer at the edge).
- Authn middleware can be added to the non-`/public/` routes without
  touching these.
- The URL itself documents what is exposed externally.

### `GET /api/knowledge/public/graph`

- Same JSON shape as `GET /api/knowledge/graph`, filtered.
- Nodes: `WHERE COALESCE(visibility, 'private') = 'public'`.
- Edges (`note_links`): both endpoints public — single SQL join on `notes`
  for `src` and `target`. The partial index does work here.
- Stub / gap nodes: excluded entirely (they leak the existence of
  unresolved private wikilinks).
- Cache: `public, s-maxage=3600, stale-while-revalidate=86400,
stale-if-error=31536000` (mirroring the existing `_GRAPH_CACHE_CONTROL`).
- ETag: derived from `(public_node_count, max(indexed_at) over public)`
  (mirrors `_graph_etag`).

### `GET /api/knowledge/public/notes/{note_id}`

- 200 with sanitized body for public notes.
- **404 (identical response) for both "doesn't exist" and "exists but
  private"** — never expose existence.
- Body sanitization (server-side, before serialization):
  - Find every `[[...]]` wikilink in the body.
  - Resolve targets via the existing slug + alias lookup.
  - If target is private OR unresolved (gap), strip the wikilink to plain
    text — keep the bracket contents minus the brackets. Bracket-text leak
    accepted in the design.
  - Implementation: `sanitize_public_body(body, private_target_ids)` in
    `visibility.py`, fully unit-testable with no DB.
- Up-front per-request query: `SELECT note_id FROM notes WHERE
COALESCE(visibility, 'private') = 'private'` → in-memory check during
  sanitization. Acceptable for V1; can target by linked-id later if it
  shows up in profiling.
- Response payload: same shape as the private endpoint minus internal-only
  fields. Audit before merge to confirm no `extra` JSONB blob leaks.

### Helper module — `projects/monolith/knowledge/visibility.py`

- `VISIBILITY_CRITERIA` — the prompt block above.
- `effective_visibility(note) -> Literal["public", "private"]` — coalescing.
- `sanitize_public_body(body, private_target_ids) -> str` — wikilink
  stripper.
- `public_graph_filter()` — SQLAlchemy clause / SQL fragment for the join
  filter.

Public routes call **only** into this helper for the visibility gate; no
scattered `if visibility == 'public'` checks anywhere else in the codebase.

### Not exposed publicly in V1

- `GET /api/knowledge/search` — embedding-driven; needs visibility-aware
  chunk filtering and rate limits. Defer to V3 (chat).
- `GET /gaps`, `/dead-letter`, `/notes/.../edit`, `/ingest`,
  `POST /notes` — internal/admin.

### Auth posture

- Public routes: no auth, behind Cloudflare per repo convention. Standard
  edge rate limits.
- Existing private routes: unchanged. This change introduces no new authn
  and modifies no existing authn — it only exposes a strictly narrower,
  filtered subset under a new prefix.

## Testing

All tests land in the V1 PR. Per repo CLAUDE.md, no local test loop —
implement, commit (Conventional Commits), push the PR branch, monitor via
`gh pr checks <n> --watch`, iterate via `mcp__buildbuddy__*` on failures.

### Unit

- `frontmatter_test.py` — visibility parsed, validated, warn-don't-fail on
  bad values (`visibility: yellow` → null + warning); round-trips through
  `_PROMOTED_KEYS`.
- `visibility_test.py` (new) — `effective_visibility` coalescing,
  `sanitize_public_body` for: no links, link to public, link to private,
  link to unresolved gap, multiple links per line, malformed `[[...]]`.
- `store_test.py` — reconciler writes `visibility` column from frontmatter
  on upsert; null frontmatter → null column.
- `models_test.py` — `Visibility` Literal accepts `public` / `private` /
  None, rejects others (assert via raw insert against the SQL CHECK).

### Router

- `GET /api/knowledge/public/graph`: only public nodes, only doubly-public
  edges, no stub/gap nodes, ETag stable across calls, ETag changes when
  the public set mutates, cache-control header correct.
- `GET /api/knowledge/public/notes/{id}`: 200 + sanitized body for public,
  identical 404 for missing and for existing-but-private, no internal
  `extra` fields leak.
- Conftest fixture seeds: `pub-A → pub-B` (kept), `pub-A → priv-X` (edge
  dropped + body wikilink stripped), `priv-Y → pub-A` (entire edge dropped
  — `priv-Y` not in public graph), gap node (excluded entirely).

### Migration

Existing migration test pattern in `chart/migrations/` — apply check,
partial index assertion, CHECK constraint round-trip via raw SQL.

### Scripts

- `add_visibility_field_test.py` — idempotent (second run is a no-op),
  skips already-keyed files, atomic write (mid-run failure does not
  truncate), parse failure logs + skips.
- `classify_visibility_backfill_test.py` — mocks `claude` subprocess;
  strict JSON parse; refuses to write report path inside the working
  tree; resumability (re-run skips already-labelled).

## Rollout

```
PR1 (single PR, all V1 code, merges to main)
  ├─ SQL migration + chart version bump (Chart.yaml + deploy/application.yaml targetRevision)
  ├─ Note model + Visibility literal
  ├─ Frontmatter parser changes
  ├─ Reconciler / store write path
  ├─ visibility.py: criteria + helpers + sanitizer
  ├─ Public routes (graph + note-by-id)
  ├─ Prompt updates (gardener atom + distill, research_writer)
  ├─ Phase-1 script (add_visibility_field)
  ├─ Phase-2 script (classify_visibility_backfill)
  └─ All tests above
   ↓ merges; ArgoCD syncs; public endpoints live but return empty
   ↓ (no notes are explicitly public yet)

Phase-1 vault commit (Joe, locally)
  └─ bazel run //...:add_visibility_field — single commit to vault repo, push
   ↓ reconciler picks up; DB column populated with nulls; serves as private

Phase-2 vault commit (Joe, locally; whenever)
  └─ bazel run //...:classify_visibility_backfill — single commit to vault, push
   ↓ reconciler updates DB; public endpoints start returning content
   ↓ smoke-test: curl /api/knowledge/public/graph and /api/knowledge/public/notes/<known-public>

V2 (separate plan): SvelteKit page under routes/public/notes/
V3 (separate plan): chat surface backed by qwen + KG animation
```

## Operational details

- **Chart version bump:** PR1 modifies `chart/migrations/`, so `Chart.yaml`
  and `deploy/application.yaml` `targetRevision` must both be bumped (per
  `feedback_chart_version_bumps.md`). The chart-version-bot will handle
  this if missed, but the implementer should do it explicitly.
- **Observability:** public endpoints emit one structured log line per
  request — `public.graph.served`, `public.note.404` (with internal-only
  reason: `not_found` vs `private_gated`), `public.note.served`. Useful
  when an alarming-looking `404_private_gated` rate spikes (= classifier
  retagged a popular note private).
- **No RBAC changes:** endpoints only read the DB; no cluster resource
  reads.
- **No image-updater changes:** no new images.
- **Revert path:** revert PR1 → migration's `ADD COLUMN` rolls back via
  existing migration tooling, partial index goes with it. If Phase 2
  produces bad labels, `git revert` the vault commit, tune criteria,
  re-run.

## Out of scope for V1

- Frontend page (V2).
- Public chat / search / embeddings (V3).
- Visibility-aware preview while editing in Obsidian.
- `needs_review` intermediate state — only revisit if classifier
  confidence proves to be a real problem in the backfill report.
- Re-classification of already-labelled notes (backfill is null-only).
- Per-chunk visibility (chunks inherit from parent note).
