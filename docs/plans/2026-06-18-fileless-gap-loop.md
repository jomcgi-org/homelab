# Fileless Gap Loop Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the knowledge-graph gap loop (detect -> classify -> research -> commit) work entirely on Postgres, and remove every remaining vault-filesystem coupling in the knowledge subsystem.

**Architecture:** A commit hook in the fileless `create_atom` core closes gap rows when a matching atom appears; `discover_gaps` becomes a Postgres-only scheduler job; classification moves to a claude.ai routine driving a new `set-gap-class` MCP tool; the research routine (already shipped) drains external gaps. All dead vault-coupled handlers and modules are deleted.

**Tech Stack:** Python, SQLModel (sync sessions), FastAPI, the in-house MCP server (`knowledge/mcp.py`), the in-house scheduler (`scheduler/api.py` + `knowledge/service.py::on_startup`), pytest with SQLite `create_all` fixtures, claude.ai routines (`projects/monolith/claude_routines/`).

**Design doc:** `docs/plans/2026-06-18-fileless-gap-loop-design.md` (read it first).

**Critical landmine (read before any commit-setting code):** the Postgres CHECK `gaps_state_class_combo` requires `state='committed'` to have `gap_class IN (external, internal, hybrid)`. SQLite test fixtures do NOT enforce CHECKs, so a NULL/`parked`-class commit passes CI and 500s in prod. Every code path that sets `committed` MUST guard the class explicitly and have a test asserting NULL-class gaps are skipped.

**Conventions:**

- No local test loop. Implement, commit; tests run on CI after push (`gh pr checks --watch`). Each task's "run the test" step means: author the test, reason it through, and rely on CI for execution.
- Scheduler handlers: `async def handler(session) -> datetime | None`; do network I/O with `await`, delegate ALL DB work to `await asyncio.to_thread(_sync_core, plain_data)` where `_sync_core` opens its own `Session(get_engine())`. Never pass the scheduler `session` into `to_thread` (semgrep `no-session-in-to-thread`). Keep a sync core taking explicit `session` for SQLite tests.
- `# gazelle:exclude knowledge` is set: any NEW test/binary under `knowledge/` must be hand-registered in `projects/monolith/BUILD`.
- Conventional Commits; commit after each task. Bump `projects/monolith/chart/Chart.yaml` + `projects/monolith/deploy/application.yaml` together in the final task.

---

## Task ordering & dependencies

1. Task 1 (commit hook) - independent, highest value.
2. Task 2 (fileless `answer_gap`) - depends on Task 1's create-atom-core understanding.
3. Task 3 (`set-gap-class` tool + function) - independent of 1/2.
4. Task 4 (fileless `discover_gaps` + handler) - independent.
5. Task 5 (classify routine YAML + skill) - depends on Task 3 (the tool it calls).
6. Task 6 (vault purge: delete dead modules + deregister handlers + strip `vault_root`) - LAST, because earlier tasks remove the live callers of the vault helpers it deletes.
7. Task 7 (chart bump + routine sync + end-of-PR review).

Implementers: read the design doc and the cited current code before editing. Defer all test execution to CI.

---

## Task 1: Commit hook - `resolve_gaps_for_note`

**Files:**

- Modify: `projects/monolith/knowledge/gaps.py` (add function near the other gap mutators)
- Modify: `projects/monolith/knowledge/mcp.py::create_atom` (call the hook after `index_note_from_raw`)
- Test: `projects/monolith/knowledge/gap_commit_hook_test.py` (new; register in `projects/monolith/BUILD`)

**Step 1: Write the failing test.** Cover: (a) an `external`/`discovered` gap whose `term` matches the new note's title is set `committed` with `note_id` + `resolved_at`; (b) a gap whose `term` matches an alias (not the title) is also committed; (c) a gap with `gap_class IS NULL` is NOT committed (stays `discovered`) - the CHECK-combo guard; (d) a gap with `gap_class='parked'` is NOT committed; (e) an already-terminal gap (`committed`/`rejected`/`parked`) is untouched; (f) no match -> no change. Use the SQLite `create_all` fixture pattern from `gap_lifecycle_test.py:28-47`.

**Step 2: Run test to verify it fails** (CI, or note expected failure: `resolve_gaps_for_note` undefined).

**Step 3: Implement.**

```python
# gaps.py
_OPEN_GAP_STATES = ("discovered", "classified", "in_review", "researching", "researched")
_COMMITTABLE_CLASSES = ("external", "internal", "hybrid")  # gaps_state_class_combo legal set

def resolve_gaps_for_note(
    session: Session,
    *,
    note_id: str,
    title: str,
    aliases: list[str] | None,
) -> list[int]:
    """Commit open gaps whose term now resolves to this note.

    Called after a note is indexed (create_atom / gardener / research routine).
    Matches gap.term (slug-normalized) against the note's note_id, title, and
    aliases. Only commits gaps whose gap_class is in the CHECK-combo legal set;
    NULL/parked-class gaps are left open (committing them violates
    gaps_state_class_combo on Postgres, which SQLite does not catch).
    """
    candidates = {note_id, _slugify(title)}
    for a in aliases or []:
        candidates.add(_slugify(a))
    rows = session.execute(
        select(Gap).where(
            Gap.deleted_at.is_(None),
            Gap.state.in_(_OPEN_GAP_STATES),
            Gap.gap_class.in_(_COMMITTABLE_CLASSES),
        )
    ).scalars().all()
    committed: list[int] = []
    now = datetime.now(timezone.utc)
    for gap in rows:
        if _slugify(gap.term) in candidates:
            gap.state = "committed"
            gap.note_id = note_id
            gap.resolved_at = now
            gap.human_verified = False
            committed.append(gap.id)
    if committed:
        session.commit()
    return committed
```

(Confirm the exact `_slugify` import and `Gap` columns against current `gaps.py`/`models.py`.)

**Step 4: Wire into `create_atom`.** In `knowledge/mcp.py::create_atom`, after the `await index_note_from_raw(...)` call (~line 651-657) and within the same `Session`, call:

```python
resolve_gaps_for_note(
    session,
    note_id=note_id,
    title=title,
    aliases=aliases or [],
)
```

Confirm `create_atom` already holds an open `Session` at that point; reuse it. Add a test in `knowledge/mcp_test.py::TestCreateAtom` style is mock-based - instead add a real-session integration assertion in the new `gap_commit_hook_test.py` that drives `resolve_gaps_for_note` directly (the create_atom mock tests stay as-is).

**Step 5: Run tests (CI).** **Step 6: Commit** `feat(monolith): commit gap rows when a matching atom is indexed`.

---

## Task 2: Fileless `answer_gap`

**Files:**

- Modify: `projects/monolith/knowledge/gaps.py::answer_gap` (~877-996): remove `_processed/` write + stub unlink + `vault_root` param; create the atom via the create_atom core; keep DB state mutations + tombstone branch.
- Modify: `projects/monolith/knowledge/mcp.py` (the `answer-gap` MCP tool) and `knowledge/router.py` (HTTP caller) to drop `vault_root`.
- Modify: `knowledge/mcp.py::create_atom` core OR factor a shared `_index_atom(...)` helper so `answer_gap` can request `source_tier: personal` + `visibility: private`.
- Test: `projects/monolith/knowledge/gap_answer_fileless_test.py` (new; register in BUILD)

**Step 1: Write failing tests.** (a) answering an `in_review` internal/hybrid gap creates a note (assert via the store) and sets gap `committed`, `human_verified=true`, with `source_tier: personal`; (b) the frontmatter-terminator guard still raises; (c) a `Tombstone - ...` answer sets `rejected` and creates NO note; (d) answering a non-`in_review` gap raises ValueError.

**Step 2: verify fails. Step 3: implement** - factor the atom-build/index from `create_atom` into a reusable `async def _index_atom(session, *, title, body, type, visibility, source_tier=None, aliases=None, edges=None) -> str` returning `note_id`; have both `create_atom` and the new `answer_gap` path call it. `answer_gap` becomes `async` (or wraps the async index via the same pattern `create_atom` uses). Extend the frontmatter dict (mcp.py ~626) to emit `source_tier` when provided.

**Step 4: tests (CI). Step 5: commit** `refactor(monolith): make answer_gap fileless via the create_atom core`.

---

## Task 3: `set-gap-class` MCP tool + transition function

**Files:**

- Modify: `projects/monolith/knowledge/gaps.py` (add `set_gap_class(session, gap_id, gap_class) -> dict`)
- Modify: `projects/monolith/knowledge/mcp.py` (add `set-gap-class` tool wrapping it; mind Context Forge's `;`-in-description sanitization - keep the description punctuation-safe, see `feedback_context_forge_description_sanitization`)
- Test: `projects/monolith/knowledge/gap_set_class_test.py` (new; register in BUILD)

**Step 1: failing tests.** From a `discovered`/NULL-class gap: `external` -> `gap_class='external'`, `state='discovered'` (unchanged); `internal`/`hybrid` -> `state='in_review'`; `parked` -> `state='parked'`; an invalid class string raises; setting a class on a non-`discovered` gap raises (or is a no-op - decide and test). Assert resulting `(state, gap_class)` is always a legal `gaps_state_class_combo` pair.

**Step 2: fails. Step 3: implement:**

```python
def set_gap_class(session: Session, gap_id: int, gap_class: str) -> dict:
    if gap_class not in ("external", "internal", "hybrid", "parked"):
        raise ValueError(f"invalid gap_class: {gap_class!r}")
    gap = _get_gap_or_raise(session, gap_id)
    if gap.state != "discovered":
        raise ValueError(f"gap {gap_id} is {gap.state!r}, expected 'discovered'")
    gap.gap_class = gap_class
    if gap_class in ("internal", "hybrid"):
        gap.state = "in_review"
    elif gap_class == "parked":
        gap.state = "parked"
        gap.resolved_at = datetime.now(timezone.utc)
    # external: leave state='discovered' for the research routine to pull
    session.commit()
    return _gap_to_dict(gap, session=session)
```

**Step 4: tests (CI). Step 5: commit** `feat(monolith): add set-gap-class MCP tool for fileless classification`.

---

## Task 4: Fileless `discover_gaps` + `knowledge.discover-gaps` handler

**Files:**

- Modify: `projects/monolith/knowledge/gaps.py::discover_gaps` (~128-390): remove `vault_root`, `write_stub`, `is_discardable`, `_rewrite_sources`, stub unlink; keep the `NoteLink`-based detection (~164-184); insert `Gap(state='discovered', gap_class=None)` for unresolved terms, skipping terms with an existing gap row (`UNIQUE(term)`).
- Modify: `projects/monolith/knowledge/service.py::on_startup` - register `knowledge.discover-gaps` -> new `discover_gaps_handler` (replaces the dead `classify-gaps`/`research-gaps` registrations, which Task 6 removes).
- Modify: `projects/monolith/knowledge/service.py` - add `discover_gaps_handler` following the to_thread pattern (sync core = the refactored `discover_gaps`).
- Test: `projects/monolith/knowledge/gap_discover_fileless_test.py` (new; register in BUILD)

**Step 1: failing tests.** Seed Notes + NoteLink rows in SQLite: (a) an unresolved `[[Term]]` with no matching note/alias -> one `Gap(discovered, NULL)`; (b) a link that resolves to an existing note_id/alias -> NO gap; (c) a term that already has a gap row -> not duplicated (UNIQUE(term)); (d) no filesystem access anywhere (the function no longer takes `vault_root`).

**Step 2: fails. Step 3: implement** the fileless `discover_gaps(session) -> int` + `discover_gaps_handler` (async wrapper -> `asyncio.to_thread`). Keep the existing SKIP-type exclusion (gaps.py:158-173). **Step 4: tests (CI). Step 5: commit** `feat(monolith): fileless discover_gaps + knowledge.discover-gaps scheduler job`.

---

## Task 5: `knowledge-classify` routine + skill

**Files:**

- Create: `projects/monolith/claude_routines/knowledge-classify.yaml`
- Create: `.claude/skills/knowledge-classify/SKILL.md`

**Step 1:** Write `knowledge-classify.yaml` mirroring `knowledge-research.yaml`: `name: Knowledge Classify (daily)`, `cron: "0 6 * * *"` (1/day; off-peak, after research), `model: claude-sonnet-4-6`, `environment: Default`, `mcp_connectors: [homelab]`, prompt that: acquires `knowledge.classify` lock; `list-gaps(state=discovered, gap_class is null)` limit ~30; for each applies the privacy rubric; calls `set-gap-class`; releases lock.

**Step 2:** Write the SKILL.md carrying the rubric ported from `knowledge/profile.py` (`RELEVANCE_KEEP`, `RELEVANCE_SKIP`, `RELEVANCE_EMPLOYER_CARVE_OUTS`) - external vs internal vs hybrid vs parked decision, privacy-conservative default (when unsure between internal/hybrid, pick the more private). Mirror the structure of `.claude/skills/knowledge-gardener/SKILL.md`.

**Step 3:** Validate the YAML against `claude_routines/schema.json` by inspection (cron pattern, required fields). **Step 4: commit** `feat(monolith): add knowledge-classify routine + skill`. (Activation is `/update-claude-routines` post-merge - Task 7.)

---

## Task 6: Vault purge (delete dead modules, deregister handlers, strip vault_root)

Do this LAST. Earlier tasks removed the live callers of the vault helpers. Work in small commits; after each deletion, grep for now-dead references and CI will catch leftovers.

**6a - Deregister + remove handlers in `knowledge/service.py::on_startup`:** remove `register_job` for `knowledge.vault-backup`, `knowledge.reconcile`, `knowledge.classify-gaps`, `knowledge.research-gaps`, `knowledge.detect-drift`; remove `vault_backup_handler`, `reconcile_handler`, `classify_gaps_handler`, `research_gaps_handler`, `detect_drift_handler`, and the vault git-clone/sentinel bootstrap (`_ensure_vault_cloned`/`_sync_ready`/`_GIT_READY` etc.). `purge_unregistered_jobs()` (api.py:86) will drop the orphaned `ScheduledJob` rows on next startup. Commit.

**6b - Delete modules:** `git rm` `knowledge/gap_stubs.py`, `knowledge/gap_classifier.py`, `knowledge/research_handler.py`, `knowledge/drift_detector.py`, `knowledge/reconciler.py` (confirm via grep no remaining runtime import; the design notes raws are S3-based so reconciler removal is safe - verify nothing else imports `reconcile`). Also delete `knowledge/research_agent.py` / `research_writer.py` / `research_audit_trail.py` only if they are exclusively used by the deleted research handler (grep first; if shared, leave). Remove their BUILD entries + tests. Commit.

**6c - Strip vault helpers from `gaps.py`:** remove `_rewrite_sources`, `_read_stub_body`, `_set_stub_status`, `_remove_stub_if_present`, the `RESEARCHING_DIR`/`write_stub` import, the `vault_root` params on `reject_gap`/`delete_gap`/`list_gaps_for_review`, and delete `approve_gap` entirely. Make `reject_gap`/`delete_gap` pure-DB; `list_gaps_for_review` drops `stub_body`. Update their tests. Commit.

**6d - Remove `approve-research-gap` + strip `vault_root` plumbing:** delete the `approve-research-gap` MCP tool (`mcp.py`) and its router endpoint; remove `vault_root` arguments threaded through `router.py`, `mcp.py`, `notes.py`, `public_router.py` for the gap endpoints; remove `VAULT_ROOT_ENV` / `DEFAULT_VAULT_ROOT` / `get_vault_root` from `service.py` once grep shows no runtime importers (the one-off scripts in "out of scope" may still import them - if so, leave the helper but confirm it is not used by any registered handler/request path; prefer deleting the scripts' imports in a separate trivial follow-up). Commit.

**6e - Drift/health endpoints:** if `router.py` exposes a drift endpoint backed by `detect_drift`, remove it (and its frontend caller if any in `ReviewCard.svelte`/private components). Grep `detect_drift`/`drift` in `frontend/src` and `router.py`. Commit.

After 6a-6e: `grep -rn "vault_root\|VAULT_ROOT\|_researching\|_processed\|RESEARCHING_DIR" projects/monolith/knowledge/*.py | grep -v _test` should return nothing in runtime code (only out-of-scope scripts, if any remain).

---

## Task 7: Chart bump, routine sync, end-of-PR review

**Step 1:** Bump `projects/monolith/chart/Chart.yaml` version + matching `projects/monolith/deploy/application.yaml` `targetRevision` (the chart-version-bot may also do this; keep them in sync).
**Step 2:** Push the branch; `gh pr create`; watch CI (`gh pr checks --watch`). Fix failures by reading BuildBuddy logs (quote the assertion before hypothesizing).
**Step 3:** One comprehensive code review of the full diff (per repo cadence - one review per merged PR, not per task).
**Step 4 (post-merge):** Run `/update-claude-routines knowledge-classify` to activate the new routine on claude.ai. Verify with `RemoteTrigger list` + a manual `run`, then check Postgres for `discovered`->classified transitions.
**Step 5:** Update memory (`project_kg_gap_drain.md`) to mark the loop closed + vault purge complete.

---

## Verification (end state)

- New `[[wikilinks]]` in indexed notes produce `discovered` gaps (Task 4).
- `knowledge-classify` routine routes them external/internal/hybrid/parked (Tasks 3+5).
- `knowledge-research` routine (already live) researches external gaps -> atoms.
- The commit hook closes the gap rows as atoms land (Task 1); manual answers close internal/hybrid fileless (Task 2).
- `grep` shows no runtime vault references in `knowledge/` (Task 6).
- CI green; routines within the 15/day budget (~14.4/day).
