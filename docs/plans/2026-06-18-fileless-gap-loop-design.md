# Fileless Gap Loop - Design

**Date:** 2026-06-18
**Status:** Approved (design); implementation plan to follow
**Branch:** `feat/fileless-gap-loop`

## Problem

The knowledge-graph "gap" loop (detect unresolved `[[wikilinks]]` -> classify ->
research -> commit a defining note) was built around the Obsidian vault
filesystem. The Obsidian->Postgres decommission (ADR 006) migrated the _gardener_
to the fileless `create-atom` path but left the **gap/research half** on the
retired vault. Symptoms observed 2026-06-17:

- Gap creation frozen since 2026-05-31 (same day vault `vault-drop` capture
  stopped). `discover_gaps` has no production caller.
- All 458 external gaps have `research_attempts=0`; none in `classified`.
- The registered handlers (`knowledge.classify-gaps`, `knowledge.research-gaps`)
  still glob `_researching/*.md` stub files and operate on `vault_root`; with the
  vault deleted they tick over an empty directory and no-op.
- `answer_gap` (the only function that sets `state='committed'`) writes
  `_processed/<slug>.md` and depends on the (also-dead) reconciler.

The `knowledge-research` claude.ai routine (PR #2667, shipped 2026-06-18) already
sidesteps this for _research_ by using `create-atom`, but it leaves gap rows open
and does not restore detection or classification.

**Guiding principle (Joe, 2026-06-18):** now that the vault is deleted, _nothing_
in the knowledge subsystem should touch it. This design completes the gap loop
fileless AND removes all remaining vault filesystem coupling (Obsidian
decommission Phase 6/7).

## New state model

Collapse the old chain
`discovered -> classified -> researching -> researched -> committed`
to:

```
discovered ─(classify routine)─> external | in_review | parked
   external ─(research routine creates atom)─┐
   in_review ─(Joe answers, fileless)────────┼─> committed
   (any atom whose title/alias matches term)─┘
   parked  = SKIP-category, terminal
```

- `external` gaps stay `state='discovered'` after classification (the research
  routine pulls `discovered`+`in_review` external; no intermediate states).
- `internal`/`hybrid` -> `state='in_review'` (await Joe in the review queue).
- `parked` -> terminal.
- The `gaps_state_class_combo` Postgres CHECK requires `state='committed'` to have
  `gap_class IN (external, internal, hybrid)`. SQLite test fixtures do NOT enforce
  CHECKs, so every code path that sets `committed` carries an explicit in-code
  guard.

## Increment 1 - fileless gap commit

**`resolve_gaps_for_note(session, note_id, title, aliases)`** (new, `gaps.py`):
after a note is indexed, find open gaps (non-terminal states) whose `term`
slug-matches the note's `note_id`/title/aliases AND whose `gap_class IN
{external, internal, hybrid}`, and set `state='committed', note_id=<new>,
resolved_at=now(), human_verified=false`. Gaps with NULL/`parked` class are
skipped (cannot legally commit).

- Called from the `create_atom` core (`mcp.py::create_atom` ->
  `indexing.py::index_note_from_raw`), so it covers BOTH the research routine and
  the gardener: any atom that defines a gap term closes that gap.
- **`answer_gap`** is rewritten to route through the `create_atom` core instead of
  writing `_processed/<slug>.md`: build the atom fileless (extend the core's
  frontmatter to carry `source_tier: personal`), set the gap committed, drop the
  reconciler dependency and the `vault_root` param. The `_is_tombstone_answer`
  branch stays (pure DB, `state='rejected'`).

## Increment 2a - fileless discovery

**`discover_gaps`** is already DB-driven for _detection_ (it reads `NoteLink` rows
joined to `Note`, resolving against existing `note_id` + aliases). The fileless
rewrite:

- **Keep** the `NoteLink`-based unresolved-link detection; insert
  `Gap(state='discovered', gap_class=NULL)` for unresolved terms, respecting
  `UNIQUE(term)` (skip terms that already have a gap row).
- **Drop** `write_stub` / `is_discardable` / stub-unlink (no `_researching/`
  files) and `_rewrite_sources` (on-disk source-body canonicalization). Dropping
  `_rewrite_sources` is acceptable: it only rewrote `[[Term]]` to canonical slugs
  in source bodies (cosmetic); detection and resolution do not need it.
  Reintroducing it fileless (mutate `notes.content` + reindex) is a separable
  later optimization, explicitly out of scope.
- **Register** a new `knowledge.discover-gaps` scheduler handler following the
  `to_thread` contract (async wrapper -> `asyncio.to_thread(_sync_core)` opening
  its own `Session(get_engine())`; never pass the scheduler's session into
  `to_thread`).

## Increment 2b - fileless classification (claude.ai routine)

Classification moves out of the pod entirely (replacing the `claude --print`
subprocess that edited stub frontmatter):

- **`knowledge-classify` routine** (claude.ai, ~1/day, fits the 15/day cap):
  `list-gaps(state=discovered, gap_class is null)` -> apply the privacy rubric ->
  `set-gap-class`. A `knowledge-classify` SKILL.md carries the rubric (ported from
  `profile.py` RELEVANCE_KEEP / RELEVANCE_SKIP / EMPLOYER_CARVE_OUTS).
- **New `set-gap-class(gap_id, gap_class)` MCP tool**: writes the Gap row directly
  (fileless) and transitions state per the model above (external->discovered,
  internal|hybrid->in_review, parked->parked), enforcing the CHECK-combo.

Budget after this change: consolidate 1 + distill 1 + gardener 4 + daily-digest 1

- loom-stpa ~0.4 + research 6 + classify 1 = ~14.4/day (under the 15/day cap).

## Full vault purge (Phase 6/7 cleanup)

All vault coupling in the knowledge subsystem is removed. Raw ingestion already
runs through S3 (`s3://knowledge/raws/`), so removing the vault handlers does not
affect new captures (a light "nothing else depends on it" check accompanies the
reconcile removal).

**Deregister + remove handlers:** `knowledge.vault-backup`, `knowledge.reconcile`,
`knowledge.classify-gaps`, `knowledge.research-gaps`, `knowledge.detect-drift`,
and the vault git-clone/sentinel bootstrap in `service.py`.

**Delete modules:** `gap_stubs.py`, `gap_classifier.py`, `research_handler.py`,
`drift_detector.py`, `reconciler.py`, plus the stub helpers
(`_read_stub_body`, `_set_stub_status`, `_remove_stub_if_present`),
`_rewrite_sources`, and all `vault_root` params in `gaps.py`.

**Remove obsolete:** `approve_gap` / `approve-research-gap` (no approval gate in
the routine model; also a `vault_root` caller). `reject_gap` / `delete_gap` stay
but go fileless (pure DB). `list_gaps_for_review` drops its `stub_body` read.

**Strip `vault_root` plumbing** from `router.py`, `mcp.py`, `notes.py`,
`public_router.py`; remove `VAULT_ROOT_ENV` / `DEFAULT_VAULT_ROOT` /
`get_vault_root` once no runtime code references them.

**Out of scope (one-off manual scripts, not runtime - trivial separate cleanup):**
`migrate_raw_bucketing.py`, `tools/classify_visibility_backfill.py`,
`tools/add_visibility_field.py`, `chat/vault_export.py`.

## Testing

- SQLite `create_all` fixtures (no migrations), matching the existing gap tests.
- **Explicit guards + tests for the CHECK-combo landmine:**
  `resolve_gaps_for_note` and `set-gap-class` assert the legal `(state,
gap_class)` combo; a unit test asserts NULL-class gaps are skipped by the commit
  hook.
- New tests: commit hook (matching + class-filtering), fileless `discover_gaps`
  (NoteLink-based insertion, UNIQUE(term) dedupe), `set-gap-class` transitions,
  fileless `answer_gap`.
- Scheduler handler test for `knowledge.discover-gaps` (sync core driven directly,
  per the monolith handler-testing convention).

## Rollout

- Chart version bump (`Chart.yaml` + `deploy/application.yaml` in sync).
- Two new routine YAMLs (`knowledge-classify.yaml`; `knowledge-research.yaml`
  already exists) + skill(s); synced via `/update-claude-routines` post-merge.
- One comprehensive end-of-PR code review; test execution deferred to CI on the
  pushed branch (no local test loop in this repo).

## Non-goals

- Reintroducing `_rewrite_sources` (source-body wikilink canonicalization) -
  separable later optimization.
- Local-vLLM classification (chose the claude.ai routine instead).
- Closing already-`parked`/NULL-class historical gaps (they remain as-is).
