# Grimoire post-extraction quality passes: implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> to implement this plan task-by-task. NOT STARTED as of 2026-07-06; recorded for
> future implementation per [ADR services/014](../decisions/services/014-grimoire-post-extraction-quality-passes.md),
> which carries the rationale and architecture. Read the ADR first.

**Goal:** Close the last measured fidelity gap in the v6 extraction (3% contradicted,
concentrated in table rows and prose detail) and merge split-name twin entities,
without any re-extraction.

**Status:** Planned, not scheduled. Both tasks are independent; either can ship alone.

---

## Task 1: evidence-grounded stat verifier (trailing job)

**Files:**
- `projects/monolith/grimoire/verify.py` (new): worklist query, evidence selection,
  DeepSeek call, verdict application, run summary.
- `projects/monolith/grimoire/jobs.py`: register `grimoire_verify_details` job body
  (same client/env pattern as `grimoire_extract_entities`; reuse
  `GRIMOIRE_EXTRACT_API_KEY` / base URL envs).
- `projects/monolith/chart/migrations/<ts>_grimoire_verification_marker.sql`: marker
  table keyed `(entity_id, verifier_version)` with status
  (`verified|corrected|nulled|unverifiable`).
- `projects/monolith/chart/values.yaml` + cronworkflow template: suspended
  manual-only cronworkflow `grimoire-verify-details` (mirror
  `grimoire-backfill-hierarchy`'s inert-schedule pattern).
- Tests: `verify_test.py` (SQLite create_all fixtures; hand-register `py_test` in
  `projects/monolith/BUILD`; grimoire is gazelle-excluded).

**Steps:**
1. Worklist: entities with verifiable structure (creature ac/hp_avg/cr +
   ability_scores, spell level, `table` rows/dice, background equipment,
   class_feature lists) lacking a marker for the current `VERIFIER_VERSION`.
2. Evidence: the entity's mention chunks filtered to stat-block/table markers
   ("Armor Class", "AC ", "Hit Points", "HP ", "Challenge", "CR ", "| " table
   pipes, "d100"/"d20" dice refs); cap ~3 chunks per call; no marker-bearing
   chunk -> status `unverifiable`, no API call.
3. Prompt (frozen + versioned like PROMPT_VERSIONS, own registry): given stored
   fields + evidence, return `{accurate: bool, corrections: {field: value},
   unverifiable: [field]}`; state explicitly that `accurate=false` with empty
   corrections is EXPECTED when evidence lacks the data. Temperature 0,
   json_object, thinking disabled.
4. Apply: corrections write through the existing enrich paths (respect JSONB
   merge semantics); unverifiable numeric/row values are nulled; every change
   counted in the run summary (`verified/corrected/nulled/unverifiable`).
5. Markers make the pass resumable; bumping `VERIFIER_VERSION` re-verifies
   deliberately.

**Verify:** run scoped (`GRIMOIRE_VERIFY_BOOK=deep-magic-5e`) first; the ADR's two
known defects (Random Damage Type d10 table, Anthropologist equipment) must come
back corrected. Then unscoped (~10-15k calls, <$1).

## Task 2: report-first alias merge

**Files:**
- `projects/monolith/grimoire/aliases.py` (new): candidate query + report emitter
  + merge executor.
- Tests: `aliases_test.py` (+ BUILD registration).
- No migration (no new tables; the report is a file/PR artifact).

**Steps:**
1. Candidate query: same `entity_type` + same `source_book`, canonicalized name A
   is a strict prefix or token-subset of name B, >=1 co-mention chunk; locations
   additionally require compatible `site`. Emit report rows with the co-mention
   snippet (markdown, sorted by book).
2. Review: report lands as a PR artifact for Joe's approval; approved pairs feed
   the executor (explicit list input, never "all candidates").
3. Executor, one transaction per pair, survivor = longer name: repoint
   `chunk_entity_mention` + `relationship` endpoints (drop rows violating the
   `(from,to,rel_type)` unique triple), key-merge detail JSONB (survivor wins),
   delete twin + its embedding rows, re-embed survivor if summary text changed.
4. Idempotent: re-running with the same pair list is a no-op (twin gone).

**Verify:** dry-run report on lost-mine-of-phandelver must surface
Gundren/Gundren Rockseeker; executor test covers edge repointing into a
duplicate-triple collision.

## Explicitly out of scope

- Any re-extraction or prompt version bump.
- Embedding-similarity alias detection (ADR alternatives: deferred).
- DM-advice capture (separate design pass with live-play UX).
