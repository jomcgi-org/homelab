# Calibration Fixes from Pruning Session — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. This repo has **no local test loop** (see "Test execution policy" below) — every "verify it passes" check happens on the pushed branch's CI run, not on your workstation. Self-review before each commit; one end-of-PR code review against the full diff (per project CLAUDE.md), not per-task reviewers.

**Goal:** Three small calibration/correctness fixes that close gaps surfaced by today's vault-pruning session — stop Discord summaries flooding the KG at the source, honor the user's `Tombstone — ` answer convention, and teach the classifier to distinguish public-figure person atoms from peer person atoms.

**Architecture:** Each fix is a 1-3 file change to existing knowledge-pipeline code, with a tight drift-detector test alongside. All three live in `projects/monolith/knowledge/`. No new migrations, no new endpoints, no schema changes. Reuses existing `_remove_stub_if_present` helper, existing test files, existing classifier-prompt slot. PR shape: 3 commits + 1 plan-doc commit, single review against full diff.

**Tech Stack:** Python 3 (FastAPI, SQLModel), pytest (SQLite fixtures via `create_all`), Bazel + BuildBuddy CI (no local test runner), Conventional Commits.

---

## Test execution policy (read this before Task 1)

This repo's BuildBuddy `workflows` pool has no darwin runners and the linux fallback is too slow/flaky to be the inner loop. **Do not** run `bazel test`, `pytest`, `pnpm test`, or any other test runner from the workstation. Instead:

- Write tests in the same commit as the implementation they cover.
- After each task's commit, leave the branch local; push everything at the end of Task 4.
- Watch CI on the PR via `gh pr checks <number> --watch`.
- If CI is red, read the failure via `mcp__buildbuddy__get_invocation` (selector: `commitSha`) → `get_target` → `get_log`. Quote the actual assertion error verbatim before hypothesising a cause.
- The "expected output" lines below describe what CI should show, not what to run locally.

Every commit message follows Conventional Commits (`fix(knowledge): …`). Branch is already created at `fix/calibration-from-pruning-session` in the worktree at `/tmp/claude-worktrees/calibration-fixes`.

---

## Repo-specific gotchas the executor must respect

1. **`# gazelle:exclude knowledge`** — `projects/monolith/BUILD` is hand-maintained for everything under `projects/monolith/knowledge/`. New test files require an explicit `py_test` entry. For this plan **all new tests go into existing test files**, so no BUILD edit is required. Verify with `git diff --stat` before each commit (BUILD should not appear).
2. **SQLite test fixtures use `create_all`, not migrations.** Tests must seed rows in the desired starting state directly — no fixture changes needed for this plan.
3. **`_remove_stub_if_present(vault_root, gap, *, action)`** is the canonical stub-cleanup helper (see `gaps.py:696`). Task 2 reuses it; do not write a parallel cleanup path.
4. **`CLASSIFIER_VERSION`** is at `gap_classifier.py:29`. Bumping it propagates to every newly-classified stub's `classifier_version` frontmatter, which is the audit trail for "which classifier produced this atom". Task 3 bumps `v2 → v3`.
5. **Conventional Commits hook**: `fix(knowledge): …`, `fix(monolith): …` — both valid. The `commit-msg` hook will reject other forms. Never `--no-verify`.
6. **Per project CLAUDE.md, this is a multi-task plan: ONE end-of-PR code review against the full diff, not per-task reviewers.** Implementers self-review before each commit; the comprehensive review runs as part of Task 4.

---

## Task 1: Exclude `_discord/` from raw-ingest move_phase

**Files:**

- Modify: `projects/monolith/knowledge/raw_ingest.py:23-37` (extend `_EXCLUDED_TOP_LEVEL`)
- Modify: `projects/monolith/knowledge/raw_ingest_test.py` (add one drift-detector test)

### Step 1 — Add `"_discord"` to `_EXCLUDED_TOP_LEVEL`

Open `projects/monolith/knowledge/raw_ingest.py`. The set starts at line 23 and currently ends with `".obsidian", ".trash",`. Insert a new entry **before** `".obsidian"` (keeps dot-prefixed entries together at the bottom). Use this exact comment block — the wording is load-bearing for the next person reading `git blame`:

```python
    # _discord/ holds per-user and per-channel summary mirrors written
    # by chat/vault_export.py. Those files are intentionally in-place
    # (overwritten on every summarisation cycle, no dated snapshots)
    # and explicitly NOT meant for KG ingest — see vault_export.py's
    # module docstring. Without this exclusion, move_phase atomically
    # renames the mirror into _raw/YYYY/MM/DD/<hash>-<slug>.md on
    # every cycle, the gardener turns each into a permanent dated atom,
    # and the KG fills up with combinatorial channel × user × cycle
    # noise. Today's vault audit (2026-05-28) deleted ~258 atoms that
    # originated this way.
    "_discord",
```

Place it after `"_trash",` and before `".obsidian",`.

### Step 2 — Add the drift-detector test

The repo has three raw-ingest test files (`raw_ingest_test.py`, `raw_ingest_discover_test.py`, `raw_ingest_move_phase_oserror_test.py`). Target `raw_ingest_test.py` — it's the general test file for module-level invariants.

Append this test (match the existing import/function style — confirm by reading the first ~20 lines first):

```python
def test_excluded_top_level_contains_discord_to_match_vault_export_intent():
    """Drift detector: _discord/ must be excluded from move_phase.

    chat/vault_export.py writes user + channel summaries to
    _discord/<channel_id>/... with an explicit "NOT for KG ingest"
    docstring. If _discord/ is removed from _EXCLUDED_TOP_LEVEL,
    move_phase will atomically rename those files into _raw/ and the
    gardener will turn each one into a permanent dated atom — exactly
    the flood the exclusion was added to stop (2026-05-28 audit
    deleted ~258 atoms produced this way).
    """
    from knowledge.raw_ingest import _EXCLUDED_TOP_LEVEL

    assert "_discord" in _EXCLUDED_TOP_LEVEL, (
        "_discord/ must be excluded from move_phase to match "
        "chat/vault_export.py's intent (see module docstring there)"
    )
```

If the file imports `_EXCLUDED_TOP_LEVEL` at module top already, drop the inline import. If it doesn't, keep the inline import as written — matches the standard pattern in this repo's test files.

### Step 3 — Self-review

- `grep -c '"_discord"' projects/monolith/knowledge/raw_ingest.py` → must be `1`.
- `git diff --stat` → exactly two files: `raw_ingest.py` and `raw_ingest_test.py`. No BUILD edit. No third file.
- Re-read the change in context: `"_discord"` is inside the `_EXCLUDED_TOP_LEVEL` set literal (between `"_trash",` and `".obsidian",`), and the comment block is immediately above the new entry, not above the closing brace.
- Mentally walk `_discover_vault_root_drops` (around line 46) with `_discord/` in the excluded set: at line 54 `if entry.name in _EXCLUDED_TOP_LEVEL: continue` short-circuits, so any directory entry named `_discord` is now skipped before its children are walked.

### Step 4 — Commit

```bash
git add projects/monolith/knowledge/raw_ingest.py projects/monolith/knowledge/raw_ingest_test.py
git commit -m "fix(knowledge): exclude _discord/ from raw-ingest move_phase

chat/vault_export.py writes Discord channel + user summary mirrors to
_discord/<channel_id>/... with an explicit 'NOT for KG ingest'
docstring, but knowledge/raw_ingest.py:move_phase didn't have
_discord/ in its exclusion list. The result: every summarisation
cycle atomically renamed the in-place mirror into _raw/YYYY/MM/DD/
where the gardener turned it into a permanent dated atom, and the KG
filled with combinatorial channel x user x cycle noise. Today's vault
audit cleaned up ~258 atoms produced this way; this stops the source.

Adds a one-line drift detector so removing _discord/ from the set
flips a test."
```

CI expectation on push (at end of Task 4): `test_excluded_top_level_contains_discord_to_match_vault_export_intent` passes; existing raw-ingest tests untouched.

---

## Task 2: `answer_gap` honors `Tombstone — ` answer convention

**Files:**

- Modify: `projects/monolith/knowledge/gaps.py` (add `_is_tombstone_answer` helper near other private helpers, add short-circuit in `answer_gap` at line 858)
- Modify: `projects/monolith/knowledge/gap_lifecycle_test.py` (5 new bare-function tests in the existing `test_answer_gap_*` cluster around lines 599-771)

### Step 1 — Add the `_is_tombstone_answer` helper

In `projects/monolith/knowledge/gaps.py`, insert this helper immediately above `def answer_gap(` (which starts at line 858). It keeps the detection logic out of the main function body and makes the test fixture obvious.

```python
def _is_tombstone_answer(answer: str) -> bool:
    """Detect the user's 'Tombstone — ...' convention on a gap answer.

    Matches the leading marker in any of the user's actual forms:
    ``Tombstone `` (capital T then ASCII space), ``Tombstone—`` (em-dash
    directly), ``Tombstone -`` (ASCII hyphen). Case-sensitive on the
    capital T — lowercased ``tombstone`` could plausibly appear in
    legitimate answers about graveyards, soft-deletes, or tombstone
    fields in storage engines, none of which the user wants
    short-circuited.
    """
    stripped = (answer or "").lstrip()
    if not stripped.startswith("Tombstone"):
        return False
    after = stripped[len("Tombstone"):]
    # space, em-dash, ASCII hyphen, or end-of-string all count.
    return after[:1] in (" ", "—", "-", "")
```

### Step 2 — Add the short-circuit in `answer_gap`

In `answer_gap` (line 858), insert the short-circuit AFTER the existing validation block (after the `if "\n---\n" in f"\n{answer}\n":` block at line 886-889) and BEFORE the `processed_root = vault_root / "_processed"` line at line 891. The validation order matters — we still want unknown-id, wrong-state, and frontmatter-terminator errors to fire before the tombstone branch.

```python
    if _is_tombstone_answer(answer):
        # User convention: "Tombstone — <reason>" means "this gap doesn't
        # deserve a content atom". Route to reject_gap semantics: gap is
        # closed, stub removed, NO atom file written. Honors the marker
        # text the user has been typing for months that the system never
        # acted on (2026-05-28 vault audit found 14 zombie atoms produced
        # by the answer-and-then-create-atom path on Tombstone-prefixed
        # answers).
        gap.state = "rejected"
        gap.human_verified = True
        gap.resolved_at = datetime.now(timezone.utc)
        _remove_stub_if_present(vault_root, gap, action="tombstoned")
        session.commit()
        session.refresh(gap)
        logger.info(
            "gaps.answer_gap: tombstoned gap_id=%d term=%r", gap_id, gap.term
        )
        return _gap_to_dict(gap, session=session)
```

All imports used here are already in scope at the top of `gaps.py`: `datetime`, `timezone`, `logger`, `_remove_stub_if_present`, `_gap_to_dict`. Confirm by `grep -n "^from datetime\|^logger\|^def _remove_stub_if_present\|^def _gap_to_dict" projects/monolith/knowledge/gaps.py` before editing.

### Step 3 — Add the five tests

Open `projects/monolith/knowledge/gap_lifecycle_test.py`. The existing `test_answer_gap_*` tests are bare functions (not class methods), located between lines 599-771. Add these five tests **in the same style** — bare functions, `(session, tmp_path)` signature, place them as a contiguous block at the END of the answer_gap cluster (after `test_answer_gap_succeeds_when_stub_missing` at line 771).

```python
# ---------------------------------------------------------------------------
# answer_gap — Tombstone- prefix routing (2026-05-28 v3 calibration)
# ---------------------------------------------------------------------------


def test_answer_gap_tombstone_em_dash_prefix_rejects_without_atom(session, tmp_path):
    """'Tombstone — <reason>' answers route to reject, no atom file written."""
    gap_id = _seed_reviewable_gap(session, term="some-term")
    result = answer_gap(
        session, gap_id, "Tombstone — vault convention, not worth a content atom", tmp_path
    )
    gap = session.get(Gap, gap_id)
    assert gap.state == "rejected"
    assert gap.human_verified is True
    assert gap.resolved_at is not None
    assert result["state"] == "rejected"
    # Critical assertion: no atom file leaked into _processed/.
    processed_root = tmp_path / "_processed"
    assert not (processed_root / "some-term.md").exists(), (
        "Tombstone-prefixed answer must NOT produce a _processed/<slug>.md file"
    )


def test_answer_gap_tombstone_space_prefix_rejects_without_atom(session, tmp_path):
    """'Tombstone ...' (space after T) also short-circuits to reject."""
    gap_id = _seed_reviewable_gap(session, term="another-term")
    answer_gap(session, gap_id, "Tombstone vault convention, skip", tmp_path)
    gap = session.get(Gap, gap_id)
    assert gap.state == "rejected"
    processed_root = tmp_path / "_processed"
    assert not (processed_root / "another-term.md").exists()


def test_answer_gap_tombstone_hyphen_prefix_rejects_without_atom(session, tmp_path):
    """'Tombstone - <reason>' (ASCII hyphen, not em-dash) also short-circuits."""
    gap_id = _seed_reviewable_gap(session, term="hyphen-term")
    answer_gap(session, gap_id, "Tombstone - vault convention, skip", tmp_path)
    gap = session.get(Gap, gap_id)
    assert gap.state == "rejected"
    processed_root = tmp_path / "_processed"
    assert not (processed_root / "hyphen-term.md").exists()


def test_answer_gap_real_answer_still_creates_atom(session, tmp_path):
    """Regression guard: a normal answer (no Tombstone prefix) still creates the atom."""
    gap_id = _seed_reviewable_gap(session, term="normal-term")
    answer_gap(
        session,
        gap_id,
        "This is a real answer that does not start with the Tombstone marker.",
        tmp_path,
    )
    gap = session.get(Gap, gap_id)
    assert gap.state == "committed"
    processed_root = tmp_path / "_processed"
    assert (processed_root / "normal-term.md").exists(), (
        "non-Tombstone answers must still produce a _processed/<slug>.md file"
    )


def test_answer_gap_lowercase_tombstone_treated_as_real_answer(session, tmp_path):
    """Regression guard: lowercase 'tombstone' (e.g. in 'tombstones in old
    graveyards…' or 'tombstone records in storage engines…') is NOT a marker
    and must produce a normal atom.
    """
    gap_id = _seed_reviewable_gap(session, term="graveyard-term")
    answer_gap(
        session,
        gap_id,
        "tombstones in old graveyards typically lean for soil-mechanics reasons.",
        tmp_path,
    )
    gap = session.get(Gap, gap_id)
    assert gap.state == "committed"
    processed_root = tmp_path / "_processed"
    assert (processed_root / "graveyard-term.md").exists()
```

**Important — verify the seed helper.** The existing tests in this file use `_seed_reviewable_gap` (search for its definition: `grep -n "_seed_reviewable_gap" projects/monolith/knowledge/gap_lifecycle_test.py`). If that helper exists and takes a `term=` kwarg, the tests above will work as written. If the helper has a different name (`_make_gap`, `_seed_gap`, etc.) or signature, **adapt the calls to match the existing pattern** — don't introduce a new helper.

The `Gap` import is presumably already at module top; if not, add `from knowledge.models import Gap`. The `answer_gap` import likewise — check the existing `from knowledge.gaps import ...` line and add `answer_gap` if absent.

### Step 4 — Self-review

- `grep -c "^def _is_tombstone_answer\b" projects/monolith/knowledge/gaps.py` → `1`
- `grep -c "def test_answer_gap_tombstone\|def test_answer_gap_real_answer\|def test_answer_gap_lowercase_tombstone" projects/monolith/knowledge/gap_lifecycle_test.py` → `5`
- `git diff --stat` → exactly two files: `gaps.py` and `gap_lifecycle_test.py`.
- Read your insertion point in `answer_gap` (around line 891 post-edit) — the tombstone short-circuit must be AFTER the `if "\n---\n" in f"\n{answer}\n":` block (so a malformed answer still errors first) and BEFORE the `processed_root = vault_root / "_processed"` setup.
- Walk one assertion mentally: the em-dash test seeds a gap with `term="some-term"`, calls `answer_gap(..., "Tombstone — ...")`, then asserts `gap.state == "rejected"` and `not (tmp_path/"_processed"/"some-term.md").exists()`. With the short-circuit in place, the function returns before `processed_root.mkdir(...)` runs, so the directory itself may not exist — `.exists()` still returns `False`, which is what we assert. Test holds.

### Step 5 — Commit

```bash
git add projects/monolith/knowledge/gaps.py projects/monolith/knowledge/gap_lifecycle_test.py
git commit -m "fix(knowledge): honor 'Tombstone -' answer convention in answer_gap

When the user types 'Tombstone — <reason>' as a gap answer, the
existing answer_gap flow wrote that text as a real atom file under
_processed/<slug>.md and marked the gap committed — producing zombie
atoms that explicitly say they shouldn't exist. Today's vault audit
manually cleaned up 14 such atoms (2026-05-28).

Adds a _is_tombstone_answer helper and a short-circuit in answer_gap
that detects the leading 'Tombstone' marker (with space, em-dash, or
ASCII hyphen) and routes to reject_gap semantics: gap.state=rejected,
human_verified=True, stub removed, no atom file written. Case-sensitive
on the capital T to avoid over-matching legitimate uses ('tombstones
in graveyards', 'tombstone records in storage engines').

Five tests cover all three marker forms + two regression guards
(normal answer still produces atom; lowercase 'tombstone' is NOT a
marker)."
```

CI expectation on push: five new tests pass; existing answer_gap tests unaffected.

---

## Task 3: Classifier prompt v3 — `person:public` vs `person:peer`

**Files:**

- Modify: `projects/monolith/knowledge/gap_classifier.py:29` (bump `CLASSIFIER_VERSION`)
- Modify: `projects/monolith/knowledge/gap_classifier.py:52-86` (extend `_CLASSIFIER_PROMPT` with new person-sub-classification block)
- Modify: `projects/monolith/knowledge/gap_classifier_test.py:190+` (add sibling drift detector after `test_classifier_prompt_routes_internal_hybrid_external_to_in_review`)

### Step 1 — Bump `CLASSIFIER_VERSION`

In `projects/monolith/knowledge/gap_classifier.py`, line 29:

Change from:

```python
CLASSIFIER_VERSION = "opus-4-7@v2"
```

to:

```python
CLASSIFIER_VERSION = "opus-4-7@v3"
```

### Step 2 — Insert the person-subclassification block in `_CLASSIFIER_PROMPT`

The prompt has these sections (from `gap_classifier.py` head numbering):

- `## The four classes` (line 38+)
- `## Rules` (line 52+)
- `## Stubs to classify` (line 88+)

Insert the new section **between `## Rules` (ends at line ~86) and `## Stubs to classify` (line 88)**. Read the file first to confirm the exact insertion line — the rules block ends with `"- If you cannot decide on a class for a stub, skip it (leave gap_class null). Do not guess."` followed by a blank line, then `## Stubs to classify`.

Insert this block (preserve the existing triple-quoted string's indentation — it's column 0):

```
## Person-atom sub-classification (when applicable)

When the gap term names a PERSON, additionally tag the resulting atom
with one of these sub-tags so the privacy review can route correctly:

- `person:public` — public historical or contemporary figures: authors,
  scientists, philosophers, public intellectuals, sports figures, etc.
  The atom can safely default to `visibility: public`. Examples:
  Daniel Kahneman, Bertrand Russell, Christopher Alexander, Andy
  Matuschak, Tom Chivers, Pierre-Simon Laplace.
- `person:peer` — friends, family members, colleagues, Discord channel
  members, or anyone Joe has a personal relationship with. The atom
  defaults to `visibility: private`. Examples: anyone whose primary
  reference is a Discord channel snapshot, anyone tagged `person` AND
  `discord`, anyone with `user-profile` or `channel-snapshot` co-tags.

If you cannot confidently distinguish (e.g. an ambiguous mononym, a
historical figure you don't recognise), default to `person:peer` —
privacy-conservative is the right error direction. The reviewer can
upgrade to `person:public` from the review queue.

```

(Note the trailing blank line — preserves visual spacing before `## Stubs to classify`.)

### Step 3 — Add the drift-detector test

In `projects/monolith/knowledge/gap_classifier_test.py`, add this test immediately after `test_classifier_prompt_routes_internal_hybrid_external_to_in_review` (line 190):

```python
def test_classifier_prompt_includes_person_public_peer_subclassification():
    """v3 adds person:public vs person:peer sub-tag distinction.

    Without this rubric in the prompt, person-atoms get over-flagged
    as private because the bare `person` tag is treated as a privacy
    signal — Pierre-Simon Laplace ends up bucketed with actual peers
    when his atom should default visibility: public. See 2026-05-28
    vault audit for the over-flagging it caused.
    """
    rendered = _CLASSIFIER_PROMPT.format(
        classifier_version=CLASSIFIER_VERSION,
        stub_list="- /tmp/example.md",
    )
    assert "person:public" in rendered, "v3 must teach the person:public sub-tag"
    assert "person:peer" in rendered, "v3 must teach the person:peer sub-tag"
    assert "Daniel Kahneman" in rendered or "Bertrand Russell" in rendered, (
        "public-figure examples must be present so the rubric is concrete "
        "for the LLM (abstract definitions alone get misapplied)"
    )
    assert CLASSIFIER_VERSION == "opus-4-7@v3", (
        "version bump propagates into stub frontmatter so future-Joe can "
        "query for which classifier produced which atom"
    )
```

`_CLASSIFIER_PROMPT` and `CLASSIFIER_VERSION` are already imported at the top of the test file (confirmed by reading the existing `test_classifier_prompt_routes_internal_hybrid_external_to_in_review` test, which references both). If for any reason they're not, add to the existing import line.

### Step 4 — Self-review

- `grep -c "CLASSIFIER_VERSION = \"opus-4-7@v3\"" projects/monolith/knowledge/gap_classifier.py` → `1`. (Note: the older `@v2` must NOT remain.)
- `grep -c "person:public\|person:peer" projects/monolith/knowledge/gap_classifier.py` → `2` (one each in the prompt).
- Re-read the prompt in context: the new `## Person-atom sub-classification` heading must sit between the closing of `## Rules` and the opening of `## Stubs to classify`. The four-class rubric and the existing Edit-tool mechanics rules MUST be untouched.
- `git diff --stat` → exactly two files: `gap_classifier.py` and `gap_classifier_test.py`.

### Step 5 — Commit

```bash
git add projects/monolith/knowledge/gap_classifier.py projects/monolith/knowledge/gap_classifier_test.py
git commit -m "feat(knowledge): classifier v3 distinguishes person:public from person:peer

The bare \`person\` tag was treated as a privacy signal by today's
visibility-classification subagent, over-flagging public historical
figures (Pierre-Simon Laplace, Tom Chivers, Christopher Alexander,
Daniel Kahneman) as private alongside actual peers (Discord friends,
family, colleagues). v3 adds a person:public vs person:peer sub-tag
the classifier emits at gap-classification time, defaulting to
person:peer when uncertain (privacy-conservative direction).

Drift detector ensures the prompt always contains both sub-tags
plus at least one public-figure example, and pins CLASSIFIER_VERSION
to opus-4-7@v3 (the value that propagates into stub frontmatter for
audit-trail queries)."
```

CI expectation on push: new drift test passes; existing prompt-routing test (`test_classifier_prompt_routes_internal_hybrid_external_to_in_review`) continues to pass; format check unaffected.

---

## Task 4: Push, PR, end-of-PR review, merge, verify

This task wraps all three fixes into one PR, runs the single end-of-PR review per project CLAUDE.md, merges, and confirms the live behaviour.

### Step 1 — Confirm branch state

```bash
cd /tmp/claude-worktrees/calibration-fixes
git status              # working tree clean
git log --oneline origin/main..HEAD
```

Expected: 3 commits (one per fix task), in this order (oldest first):

```
<sha> fix(knowledge): exclude _discord/ from raw-ingest move_phase
<sha> fix(knowledge): honor 'Tombstone -' answer convention in answer_gap
<sha> feat(knowledge): classifier v3 distinguishes person:public from person:peer
```

Plus the plan-doc commit if it landed first (it should — writing-plans saves the plan as the first commit on the branch).

### Step 2 — Push and open the PR

```bash
git push -u origin fix/calibration-from-pruning-session

gh pr create --title "fix(knowledge): three calibration fixes from 2026-05-28 pruning session" --body "$(cat <<'EOF'
## Summary

Three calibration / correctness fixes that close gaps surfaced by today's vault-pruning session (where ~545 atoms were soft-deleted). All three address the source rather than the downstream pruning cost.

1. **`_discord/` exclusion from raw-ingest** — \`chat/vault_export.py\` writes Discord summary mirrors in-place with an explicit \"NOT for KG ingest\" docstring, but \`knowledge/raw_ingest.py:move_phase\` was renaming them into \`_raw/YYYY/MM/DD/\` on every cycle. Today deleted ~258 atoms produced this way.

2. **\`answer_gap\` \`Tombstone — \` short-circuit** — user convention for \"this gap doesn't deserve a content atom\" was creating zombie atoms instead. Today deleted 14 such atoms.

3. **Classifier v3: \`person:public\` vs \`person:peer\`** — bare \`person\` tag was over-flagging public historical figures as private during privacy review (Pierre-Simon Laplace ended up bucketed with peers).

## Test plan

- [ ] CI green on the branch (\`gh pr checks <number> --watch\`)
- [ ] After merge + monolith rollout, observe \`_discord/<channel_id>/users/*.md\` files persisting in-place over ~24 hours (not vanishing into \`_raw/\` on every summarisation cycle)
- [ ] Answer one real gap with \`Tombstone — test\` and confirm NO file appears in \`_processed/<slug>.md\`; confirm gap state moves to \`rejected\`
- [ ] After next classifier tick, confirm \`monolith-list-gaps\` shows newly-classified gaps stamped with \`pipeline_version: opus-4-7@v3\`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Capture the PR number from the output; you'll need it for `gh pr checks`.

### Step 3 — Watch CI

```bash
gh pr checks <number> --watch
```

If CI is red, read the failure via:

```
mcp__buildbuddy__get_invocation (selector: commitSha)
  -> get_target  (find the failing target)
  -> get_log
```

Quote the actual assertion or exception message verbatim before proposing a fix. Do not attribute failures to flake/infra without ruling out a real test failure first (per project CLAUDE.md).

### Step 4 — One end-of-PR code review (per project CLAUDE.md)

Once CI is green, run **one comprehensive code review** against the full diff (NOT per-task). Either dispatch `superpowers:requesting-code-review` for an independent agent, or walk the diff manually via:

```bash
git diff origin/main...HEAD
```

Confirm:

- Files touched are exactly the ones each task's "Files" block enumerates (no drift).
- No accidental changes to `move_phase`'s logic itself, the gardener, the reconciler, or any other part of the raw-ingest path. (Only the `_EXCLUDED_TOP_LEVEL` set should change in `raw_ingest.py`.)
- No accidental changes to other gap verbs (`reject_gap`, `verify_gap`, `reopen_gap`, `approve_gap`). Only `answer_gap` + the new helper.
- Classifier prompt's four-class rubric (`## The four classes`) and Edit-tool mechanics (`## Rules`) untouched — the new section is purely additive.
- No `bazel test` / `pytest` invocations leaked into commit messages.
- No BUILD edits.
- No new files outside `docs/plans/`.

Apply any fixes as additional commits on the same branch (NOT amends), push, re-watch CI.

### Step 5 — Merge

This repo only allows rebase-merge:

```bash
gh pr merge --rebase
```

Or hands-off:

```bash
gh pr merge --auto --rebase
```

### Step 6 — Verify the live rollout

After the monolith pod recycles (ArgoCD auto-syncs ~5-10s after merge; rollout takes ~30-60s):

1. **`_discord/` exclusion live-check**: in any monolith pod (or via `kubectl exec`), confirm `_discord/<channel_id>/users/*.md` files are still present and have an `updated_at` timestamp from the most recent summariser tick (i.e. they're being overwritten in place, NOT being moved to `_raw/`). Equivalent shell:

   ```bash
   ls -la /vault/_discord/*/users/ 2>/dev/null | head -10
   ls -la /vault/_raw/$(date +%Y/%m/%d) 2>/dev/null | grep -E "channel|users" | head -10
   ```

   First should be populated; second should NOT contain new channel/user-summary entries dated post-merge.

2. **Tombstone short-circuit live-check**: open `/private/review?tab=gaps&mode=pending`, find an internal/hybrid gap to acknowledge, answer with `Tombstone — test of v3 short-circuit`, submit. Confirm in `monolith-list-gaps` that the gap moved to `state=rejected` and that no atom file appeared in `_processed/`. Then UI: confirm the gap is gone from the pending queue.

3. **Classifier v3 live-check**: wait for the next `knowledge.classify-gaps` tick (~5 min), then query `monolith-list-gaps state=in_review limit=10` and confirm the freshest rows show `pipeline_version: opus-4-7@v3`. (Older rows will still be at v2 — that's correct; v3 only stamps newly-classified stubs.)

---

## Out of scope (do not let any task expand into these)

- **Backfilling existing `person-*` atoms** with `person:public` or `person:peer` sub-tags. v3 only stamps newly-classified atoms going forward; the existing person-atom cluster (currently in `_processed/`) keeps its bare `person` tag until a separate one-shot backfill pass.
- **Cleaning up orphaned `_raw/YYYY/MM/DD/<hash>-channel.md` raws** that came from `_discord/` mirrors. The fix stops new ones from being created; the existing ones can be `mv`'d to `_trash/_raws/` (or just deleted) in a separate one-shot operation. Atoms derived from those raws were mostly already trashed in today's pruning; the residue can be handled by the visibility-review queue later.
- **Deleting remaining Discord-tagged atoms in `_processed/`.** Those ~150 atoms are in the privacy-cluster B1/B2 territory; out of scope here.
- **Codifying the public-default visibility heuristic in code** (the calibration we made in memory today — `feedback_privacy_public_default.md`). That's a separate, larger change to the visibility-classifier prompt and possibly to `visibility` defaults in the note-creation paths. Keep it as memory feedback for now.

## Definition of done

- [ ] All three fixes committed in one PR, each as a separate commit with the exact commit message above.
- [ ] All new tests + existing tests green on the branch's CI run.
- [ ] One end-of-PR code review completed against the full diff.
- [ ] PR rebase-merged to main.
- [ ] Live rollout verified per Task 4 Step 6 (each of the three sub-checks).
