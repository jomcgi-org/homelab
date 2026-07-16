# Lane B — Profile-as-Python-Module + answer_gap Visibility Fix — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. This repo has **no local test loop** (see "Test execution policy" below) — every "verify it passes" check happens on the pushed branch's CI run, not on your workstation. Self-review before each commit; one end-of-PR code review against the full diff (per project CLAUDE.md), not per-task reviewers.

**Goal:** Move Joe's relevance + privacy profile from `joe-profile.md` (markdown rubric file) to a typed Python module (`profile.py`), delete the markdown file, and close the last null-visibility regrowth path by making `answer_gap` set `visibility: private` on the atoms it creates.

**Architecture:** New module `projects/monolith/knowledge/profile.py` carries `IDENTITY`, `RELEVANCE_KEEP`, `RELEVANCE_SKIP`, `VISIBILITY_CRITERIA`, `PRIVATE_CATEGORIES`, plus `PROFILE_VERSION` / `PROFILE_UPDATED` audit-trail constants. `visibility.py` re-exports `VISIBILITY_CRITERIA` from `profile.py` so existing consumers (gardener distill prompts at `gardener.py:161, 197`) keep working unchanged. `joe-profile.md` is deleted in the same commit so there is never a window where two sources are live. `gaps.py:answer_gap` adds `"visibility": "private"` to the atom-creation frontmatter dict.

**Tech Stack:** Python 3 (FastAPI, SQLModel), pytest (SQLite fixtures via `create_all`), Bazel + BuildBuddy CI (no local test runner), Conventional Commits.

---

## Test execution policy (read this before Task 1)

This repo's BuildBuddy `workflows` pool has no darwin runners and the linux fallback is too slow/flaky to be the inner loop. **Do not** run `bazel test`, `pytest`, `pnpm test`, or any other test runner from the workstation. Instead:

- Write tests in the same commit as the implementation they cover.
- After each task's commit, leave the branch local; push everything at the end of Task 5.
- Watch CI on the PR via `gh pr checks <number> --watch`.
- If CI is red, read the failure via `mcp__buildbuddy__get_invocation` (selector: `commitSha`) → `get_target` → `get_log`. Quote the actual assertion error verbatim before hypothesising a cause.

Every commit message follows Conventional Commits (`refactor(knowledge): …`, `fix(knowledge): …`). Branch is already created at `feat/visibility-defaults-from-profile` in the worktree at `/tmp/claude-worktrees/visibility-defaults`.

---

## Repo-specific gotchas the executor must respect

1. **`# gazelle:exclude knowledge`** — `projects/monolith/BUILD` is hand-maintained for everything under `projects/monolith/knowledge/`. **A new `.py` file requires a BUILD entry** for its test partner. Plan covers this in Task 1.
2. **SQLite test fixtures use `create_all`, not migrations.** Tests must seed rows in their desired starting state directly — no fixture changes needed for this plan.
3. **`VISIBILITY_CRITERIA` has 4 known consumers** verified by grep:
   - `gardener.py:20` imports it
   - `gardener.py:161, 197` inline it via `{VISIBILITY_CRITERIA}` placeholder + `.replace(...)`
   - `gardener_distill_test.py:30` asserts `VISIBILITY_CRITERIA.strip() in rendered`
   - `visibility_test.py:8` imports it; lines 75-76 assert phrases
     All four MUST keep working after the refactor. `visibility.py` re-exports the constant to preserve backward compatibility.
4. **`joe-profile.md` lives at `projects/monolith/knowledge/joe-profile.md`** (shipped this morning via PR #2371, merge commit `ed8830fa54`). Deleted in Task 4 of this plan.
5. **Conventional Commits hook**: `commit-msg` rejects non-ASCII characters in commit title. Use ASCII-only titles (em-dash `—` is OUT; use `-` instead). Commit messages below comply.
6. **Per project CLAUDE.md, this is a multi-task plan: ONE end-of-PR code review against the full diff, not per-task reviewers.** Implementers self-review before each commit; the comprehensive review runs as part of Task 5.

---

## Task 1: Create `projects/monolith/knowledge/profile.py`

**Files:**

- Create: `projects/monolith/knowledge/profile.py`
- Create: `projects/monolith/knowledge/profile_test.py`
- Modify: `projects/monolith/BUILD` (add `py_test` entry for the new test file; the new source file is picked up by the existing `monolith_backend` `py_library` glob — verify by grep before assuming)

### Step 1 — Pre-work: check BUILD file structure

`grep -nE "knowledge/.*\.py|monolith_backend" projects/monolith/BUILD | head -20` — confirm that:

- The `monolith_backend` `py_library` (or similar) uses a glob like `srcs = glob(["knowledge/**/*.py"])` so the new `profile.py` is auto-included. If it lists files explicitly, ADD `profile.py` to the list.
- Locate an existing test entry (e.g. `knowledge_visibility_test`) to model the new `knowledge_profile_test` after.

### Step 2 — Create `profile.py`

Create `projects/monolith/knowledge/profile.py` with this exact content. The structure is locked from the design conversation; do not refactor or rename constants:

```python
"""Joe McGinley's relevance + privacy profile as typed Python constants.

Single source of truth for what's worth keeping in the vault (relevance)
and what's safe to publish (privacy). Read by classifier prompts,
visibility helpers, and ad-hoc classification subagents.

Replaces the prior joe-profile.md file (PR #2371) -- all rubric content is
Python to avoid markdown-vs-code drift and to enable importable, testable
constants. Edits go through PR review. Bump PROFILE_VERSION on substantive
changes so downstream consumers can pin or refuse stale rubrics.

## How to use this profile

For agents classifying atoms:
- Import the constants directly (RELEVANCE_KEEP, PRIVATE_CATEGORIES, etc.).
- For each atom, score against BOTH the relevance KEEP/SKIP tables AND
  the privacy categories.
- Privacy criteria are binding constraints; relevance is a heuristic.
- When uncertain on privacy -> 'private' (NOT 'public').

For classifier prompt code (gap_classifier.py, visibility.py):
- Reference these constants directly rather than hardcoding rubrics.
- Pin against PROFILE_VERSION so prompt regressions surface as test
  failures rather than silent classification drift.

For Joe (future-me):
- If a category here has not been updated in 6 months and your work
  has obviously shifted, something is stale. The two-question test
  is the constant; the seed lists evolve. Bump PROFILE_VERSION and
  PROFILE_UPDATED whenever you make a substantive change.
"""

from __future__ import annotations

PROFILE_VERSION = "2"
PROFILE_UPDATED = "2026-05-28"

IDENTITY = """\
(redacted: role and work history live in the private values file, not in
this repo). Public CV: https://jomcgi.dev.
"""

RELEVANCE_KEEP: list[dict[str, str]] = [
    {
        "domain": "eBPF / kernel observability",
        "signals": "cgroup_skb, BPF maps, Hubble, kfunc, BTF, bpftrace",
    },
    {
        "domain": "Kubernetes at scale",
        "signals": "multi-cluster, control-plane internals, operators (Go), CRDs, admission webhooks",
    },
    {
        "domain": "Service mesh",
        "signals": "Linkerd primarily, Envoy, Gateway API, Cloudflare ZeroTrust",
    },
    {
        "domain": "Observability / SLO",
        "signals": "OTel internals, SigNoz, Honeycomb, Prometheus, RCA, STAMP/STPA",
    },
    {
        "domain": "FinOps / cost attribution",
        "signals": "eBPF cost meters, AWS CUR, Orb, Iceberg/Snowflake cost, OpenCost (rejected; know why)",
    },
    {
        "domain": "Data platform",
        "signals": "SQLMesh, dbt, Postgres pgvector/HNSW, Neo4j, Iceberg, BigQuery, DuckDB",
    },
    {
        "domain": "Build systems",
        "signals": "Bazel/Starlark, BuildBuddy RBE, rules authoring, aspects",
    },
    {
        "domain": "AI infra",
        "signals": "vLLM, MCP, Claude/Anthropic, agent orchestration, prompt caching, transformer internals (Mamba, RoPE, SwiGLU, RMSNorm)",
    },
    {
        "domain": "Security / AppSec / Semgrep",
        "signals": "rules, AST/dataflow, taint mode, CWE, supply-chain (defensive)",
    },
    {
        "domain": "Knowledge graphs / PKM",
        "signals": "Obsidian core patterns, Andy Matuschak, spaced repetition, Tools for Thought",
    },
    {
        "domain": "Coffee",
        "signals": "origin/processing, brewing, cupping, roasters, lever espresso (Decent etc.)",
    },
    {
        "domain": "Philosophy - analytic/skeptical/Stoic only",
        "signals": "Sextus Empiricus, Pyrrho, Kahneman, Tversky, Kripke, Wittgenstein, Stoics, philosophy of science, decision theory",
    },
    {
        "domain": "Career / leadership / SRE methodology",
        "signals": "refactoring, TDD, organizational design, distributed systems theory",
    },
]

RELEVANCE_SKIP: list[dict[str, str]] = [
    {
        "category": "Frontend framework fads",
        "examples": "React/Vue/Vite/Remix deep dives NOT related to monolith Svelte 5 work",
    },
    {
        "category": "AI-IDE vendor profiles",
        "examples": "Cursor, Replit, Codecademy, Windsurf, Codespaces",
    },
    {
        "category": "Pop culture / gaming / consumer tech",
        "examples": "Valve, Dota, Stadia, watchOS, Half-Life, Counter-Strike, Roblox, Wordle",
    },
    {
        "category": "Vendor marketing / M&A trivia",
        "examples": "mergers, acquisitions, IPOs of non-strategic companies",
    },
    {
        "category": "Wikipedia-stub web fundamentals",
        "examples": "HTTP, DNS, HTML, SVG, WebRTC at primer level",
    },
    {
        "category": "Religious / theological / patristic content",
        "examples": "Christology, Gospels, Church Fathers, apologetics - NOT analytic philosophy",
    },
    {
        "category": "Pop history / political history",
        "examples": "FDR-era, US politics, EU institutions, Brexit, art/music/poetry/literature (Lorca/Blake/Ellington/Whitman), classical/patristic history, MBTI/Jung, pop-science Dawkins-tier",
    },
    {
        "category": "Obsidian PLUGIN-specific minutiae",
        "examples": "DataviewJS plugin tutorials, plugin security notes for plugins not in use",
    },
]

# Carve-outs from RELEVANCE_SKIP:
# - Employer-specific carve-outs are private configuration; the list of
#   named employers stays out of this repo.
RELEVANCE_EMPLOYER_CARVE_OUTS = (
    "(redacted: per-employer keep/skip rules live in the private values "
    "file, not in this repo)"
)

VISIBILITY_CRITERIA = """\
## Visibility (REQUIRED frontmatter field)

Every note MUST set `visibility: public` or `visibility: private`.
This controls whether the note appears on Joe's public website.

Default to `private` whenever you are uncertain.

Mark `public` when the note is about:
- General engineering concepts, principles, heuristics (DORA, Conway's Law,
  blameless postmortems, etc.) -- anything you'd find in a textbook, blog,
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
- Active tasks, daily/weekly journals, blockers -- anything operational
  about Joe's current work.

Edge cases:
- An atom about a generally-applicable pattern that includes a
  workplace-specific example: rewrite the example out and mark public,
  OR keep the example and mark private. Do not mark public with the
  example intact.
- A fact about an external library mentioned during a private incident:
  the fact is public, the incident framing is private -- split into two
  notes if needed.

When in doubt: `private`.
"""

PRIVATE_CATEGORIES: list[dict[str, list[str] | str]] = [
    {
        "name": "Job search / career mobility",
        "seeds": [
            "job-search", "interview", "recruiter", "offer", "resignation",
            "looking for", "application", "FAANG", "LeetCode-prep",
            "take-home", "reference-request",
        ],
    },
    {
        "name": "Compensation",
        "seeds": [
            "salary", "comp", "equity", "RSU", "vesting", "bonus", "raise",
            "total-comp", "levels.fyi", "negotiation",
        ],
    },
    {
        "name": "Performance / 1-on-1 / manager dynamics",
        "seeds": [
            "performance-review", "1-on-1", "PIP", "manager-critique",
            "frustrated with", "concerns about", "peer-feedback",
            "calibration",
        ],
    },
    {
        "name": "Current-employer internals",
        "seeds": [
            "Semgrep customer specifics beyond what's in the CV",
            "internal Semgrep roadmaps not yet public",
            "internal headcount/financials",
            "internal disputes",
        ],
    },
    {
        "name": "Friends, family, peers",
        "seeds": [
            "Brian", "Bjeu", "bjeambusher", "rossdd", "ochinchin",
            "scotscottmca",
            "family members",
            "Discord channel snapshots/profiles",
            "person:peer",
            "anyone whose primary reference is a Discord channel",
        ],
    },
    {
        "name": "Health (mental or physical)",
        "seeds": [
            "therapy", "therapist", "diagnosis", "medication", "anxiety",
            "depression", "sleep issues", "sobriety", "ADHD",
        ],
    },
    {
        "name": "Personal finance",
        "seeds": [
            "net worth", "mortgage", "debt", "savings rate", "FIRE plans",
            "specific account balances", "tax strategy",
        ],
    },
    {
        "name": "Relationships / dating",
        "seeds": [
            "partner", "dating", "relationship-issues", "breakup",
            "frank-dating-*",
        ],
    },
    {
        "name": "Glasgow / Vancouver civic personal",
        "seeds": [
            "local political views",
            "neighbour disputes",
            "anything tying Joe to a specific physical address or routine",
        ],
    },
]

# Asymmetric error preference: wrong-private = a few unnecessary clicks
# later. wrong-public = an employer/colleague/family member sees something
# they shouldn't, possibly indexed at public.jomcgi.dev. When in doubt,
# private. Privacy-conservative is the right error direction.
ASYMMETRIC_ERROR_PREFERENCE = "private"

__all__ = [
    "PROFILE_VERSION",
    "PROFILE_UPDATED",
    "IDENTITY",
    "RELEVANCE_KEEP",
    "RELEVANCE_SKIP",
    "RELEVANCE_EMPLOYER_CARVE_OUTS",
    "VISIBILITY_CRITERIA",
    "PRIVATE_CATEGORIES",
    "ASYMMETRIC_ERROR_PREFERENCE",
]
```

### Step 3 — Create `profile_test.py`

Create `projects/monolith/knowledge/profile_test.py`. Five tests pinning the structure and load-bearing values:

```python
"""Tests for knowledge.profile: structure invariants + load-bearing values."""

from __future__ import annotations

import pytest

from knowledge.profile import (
    ASYMMETRIC_ERROR_PREFERENCE,
    IDENTITY,
    PRIVATE_CATEGORIES,
    PROFILE_UPDATED,
    PROFILE_VERSION,
    RELEVANCE_KEEP,
    RELEVANCE_SKIP,
    VISIBILITY_CRITERIA,
)


def test_profile_version_is_string_and_present():
    """Version pin must exist and be a non-empty string (consumers can pin
    against this, refusing stale rubrics if they re-pin against an older
    version)."""
    assert isinstance(PROFILE_VERSION, str)
    assert PROFILE_VERSION


def test_profile_updated_is_iso_date():
    """PROFILE_UPDATED must be a parseable YYYY-MM-DD string so consumers
    can age-check the rubric."""
    from datetime import date

    parsed = date.fromisoformat(PROFILE_UPDATED)
    assert parsed.year >= 2026


def test_relevance_keep_rows_have_required_keys():
    """Every relevance-keep row must have 'domain' + 'signals' so the
    structure stays iterable for future classifier code."""
    for i, row in enumerate(RELEVANCE_KEEP):
        assert "domain" in row, f"row {i} missing 'domain'"
        assert "signals" in row, f"row {i} missing 'signals'"
        assert row["domain"], f"row {i} 'domain' is empty"
        assert row["signals"], f"row {i} 'signals' is empty"


def test_relevance_skip_rows_have_required_keys():
    """Every relevance-skip row must have 'category' + 'examples'."""
    for i, row in enumerate(RELEVANCE_SKIP):
        assert "category" in row, f"row {i} missing 'category'"
        assert "examples" in row, f"row {i} missing 'examples'"


def test_private_categories_have_name_and_seeds():
    """Every private category must declare a name plus a non-empty seeds list."""
    for i, cat in enumerate(PRIVATE_CATEGORIES):
        assert "name" in cat and cat["name"], f"category {i} missing/empty 'name'"
        assert "seeds" in cat, f"category {i} missing 'seeds'"
        seeds = cat["seeds"]
        assert isinstance(seeds, list), f"category {i} seeds must be a list"
        assert seeds, f"category {i} 'seeds' is empty"


def test_visibility_criteria_contains_load_bearing_phrases():
    """Drift detector: VISIBILITY_CRITERIA is inlined into the gardener
    distill prompts (gardener.py:161, 197). These specific phrases must
    survive any refactor of the criteria text -- if any disappears, the
    gardener LLM loses guardrails."""
    assert "visibility: public" in VISIBILITY_CRITERIA
    assert "visibility: private" in VISIBILITY_CRITERIA
    assert "When in doubt" in VISIBILITY_CRITERIA


def test_identity_mentions_career_thesis():
    """Identity must contain the career-thesis phrase so future relevance
    classifiers can tie-break on it ('does this atom serve the
    remove-complexity-for-other-engineers frame?')."""
    assert "remove complexity for other engineers" in IDENTITY.lower()


def test_asymmetric_error_preference_is_private():
    """Profile establishes that 'when in doubt, private' is the binding
    asymmetric-error rule. Pinned here so a refactor that flips it to
    'public' is loud."""
    assert ASYMMETRIC_ERROR_PREFERENCE == "private"
```

### Step 4 — Register the test in BUILD

In `projects/monolith/BUILD`, find an existing knowledge test (e.g. `knowledge_visibility_test`) and add a sibling entry for `knowledge_profile_test`. Match the surrounding style exactly:

```python
py_test(
    name = "knowledge_profile_test",
    srcs = ["knowledge/profile_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)
```

(Adjust the `deps` if the existing knowledge tests use a different combination — match what `knowledge_visibility_test` uses, since the new test imports from the same package.)

If `monolith_backend`'s sources are declared via an explicit list rather than a glob, also add `"knowledge/profile.py"` to its `srcs`. Verify by grep before editing.

### Step 5 — Self-review

- `python3 -c "import ast; ast.parse(open('projects/monolith/knowledge/profile.py').read())"` — sanity-parse the new file (allowed; AST parse is not a test runner).
- `grep -c "^def test_" projects/monolith/knowledge/profile_test.py` → `8`
- `grep -n "knowledge_profile_test" projects/monolith/BUILD` → exactly 1 match
- `git diff --stat` → 3 files: `profile.py`, `profile_test.py`, `BUILD`

### Step 6 — Commit

```bash
git add projects/monolith/knowledge/profile.py projects/monolith/knowledge/profile_test.py projects/monolith/BUILD
git commit -m "feat(knowledge): add profile.py as Python-native rubric module

Single source of truth for Joe's relevance + privacy profile as typed
Python constants:
- IDENTITY (str)
- RELEVANCE_KEEP / RELEVANCE_SKIP (list[dict])
- VISIBILITY_CRITERIA (str, kept as prompt-inlinable string for the
  gardener distill prompts at gardener.py:161, 197)
- PRIVATE_CATEGORIES (list[dict[str, list[str]]])
- PROFILE_VERSION / PROFILE_UPDATED (audit-trail constants)
- ASYMMETRIC_ERROR_PREFERENCE = 'private' (pinned binding rule)

Replaces joe-profile.md (PR #2371) -- markdown rubric is being deleted
in Task 4 of this PR. All rubric content is Python to avoid
markdown-vs-code drift and to enable importable, testable constants.
visibility.py will re-export VISIBILITY_CRITERIA from this module in
Task 2 so existing consumers (gardener.py) keep working unchanged.

Eight tests pin structure invariants (keys present, seeds non-empty,
version parseable) and load-bearing phrases (visibility: public/private
in the criteria, career-thesis phrase in IDENTITY, asymmetric error
preference = private)."
```

---

## Task 2: Re-export `VISIBILITY_CRITERIA` from `profile.py` via `visibility.py`

**Files:**

- Modify: `projects/monolith/knowledge/visibility.py` (replace the literal `VISIBILITY_CRITERIA` block with an import + re-export)
- Modify: `projects/monolith/knowledge/visibility_test.py` (widen two phrase-pinning assertions)

### Step 1 — Read the current state

`sed -n '1,60p' projects/monolith/knowledge/visibility.py` — confirm:

- The current literal `VISIBILITY_CRITERIA = """\\n## Visibility...When in doubt: \`private\`.\\n"""` starts at line 17.
- Imports at the top include `re`, `Iterable`, `Literal`, `ColumnElement`, and `Note`.

`sed -n '70,85p' projects/monolith/knowledge/visibility_test.py` — confirm the existing two assertions you'll be widening:

```python
assert "visibility: public" in VISIBILITY_CRITERIA
assert "When in doubt: `private`." in VISIBILITY_CRITERIA
```

### Step 2 — Replace the literal with a re-export

In `projects/monolith/knowledge/visibility.py`, delete the entire `VISIBILITY_CRITERIA = """..."""` block (lines ~17-58). In its place, add a single import line near the top of the file (right after the existing `from knowledge.models import Note` line):

```python
# Re-export VISIBILITY_CRITERIA from the canonical profile module so
# existing consumers (gardener.py:161, 197 inline {VISIBILITY_CRITERIA};
# gardener_distill_test.py:30 asserts it in rendered prompts; the public
# review queue references it) keep importing from knowledge.visibility
# without modification. The rubric itself lives once, in profile.py.
from knowledge.profile import VISIBILITY_CRITERIA  # noqa: F401 (re-export)
```

The `noqa: F401` prevents flake/ruff from removing the "unused" import — it IS used, by every downstream consumer.

If the module already has an `__all__` list, ensure `"VISIBILITY_CRITERIA"` is present so the re-export is explicit.

### Step 3 — Widen the existing test assertions

In `projects/monolith/knowledge/visibility_test.py`, find the two lines (around 75-76):

```python
assert "visibility: public" in VISIBILITY_CRITERIA
assert "When in doubt: `private`." in VISIBILITY_CRITERIA
```

Replace with:

```python
assert "visibility: public" in VISIBILITY_CRITERIA, (
    "criteria must teach the public visibility value"
)
assert "When in doubt" in VISIBILITY_CRITERIA, (
    "criteria must teach the privacy-conservative rule"
)
assert "private" in VISIBILITY_CRITERIA.lower(), (
    "criteria must teach the private visibility value"
)
```

The second assertion drops the backticks-and-period exact match (the profile uses `When in doubt: \`private\`.`— but if profile.py's wording ever shifts to`When in doubt -> private.` or similar, we want the test to pass on the intent, not the punctuation).

### Step 4 — Add an explicit re-export drift detector

Append to `visibility_test.py`:

```python
def test_visibility_criteria_is_re_exported_from_profile_module():
    """Drift detector: visibility.py's VISIBILITY_CRITERIA must be the
    SAME object as profile.py's, not a hand-maintained copy.

    Catches the regression where someone re-inlines the literal in
    visibility.py because they couldn't find where it lived. The shared
    object identity guarantees no drift can occur even if profile.py is
    edited; both consumers see the same updated string.
    """
    from knowledge.profile import VISIBILITY_CRITERIA as profile_criteria
    from knowledge.visibility import VISIBILITY_CRITERIA as visibility_criteria

    assert profile_criteria is visibility_criteria, (
        "visibility.py must re-export VISIBILITY_CRITERIA from "
        "knowledge.profile (not maintain a separate copy)."
    )
```

### Step 5 — Self-review

- `grep -c 'VISIBILITY_CRITERIA = """' projects/monolith/knowledge/visibility.py` → `0` (literal is gone)
- `grep -c 'from knowledge.profile import VISIBILITY_CRITERIA' projects/monolith/knowledge/visibility.py` → `1`
- `grep -c "When in doubt:" projects/monolith/knowledge/visibility_test.py` → `0` (the exact-match assertion is gone; replaced with `When in doubt` substring)
- `grep -c "def test_visibility_criteria_is_re_exported_from_profile_module" projects/monolith/knowledge/visibility_test.py` → `1`
- `git diff --stat` → 2 files: `visibility.py`, `visibility_test.py`. **No BUILD edit** — modifying existing tests doesn't require one.
- Read the gardener call sites mentally: `gardener.py:161` has `{VISIBILITY_CRITERIA}` placeholder in a prompt template, `.replace("{VISIBILITY_CRITERIA}", VISIBILITY_CRITERIA)` interpolates the imported constant. Re-export preserves the import path; behaviour identical.

### Step 6 — Commit

```bash
git add projects/monolith/knowledge/visibility.py projects/monolith/knowledge/visibility_test.py
git commit -m "refactor(knowledge): re-export VISIBILITY_CRITERIA from profile.py

The visibility criteria block previously lived as a literal Python
string in visibility.py (lines 17-58), duplicating what now lives in
projects/monolith/knowledge/profile.py (added in this PR's Task 1).
The module's own docstring warned about exactly this drift risk:
'Drift between the criteria the LLM sees and the criteria the routes
enforce is the most common way these systems leak.'

Eliminates the duplicate by re-exporting from knowledge.profile.
Existing import paths (gardener.py:20, gardener_distill_test.py:13)
keep working unchanged because visibility.py still exposes the
constant. A new drift detector pins the SHARED-OBJECT-IDENTITY
property so any future refactor that re-inlines the literal fails
the test (rather than silently drifting).

Two existing assertions in visibility_test.py that pinned exact
literal-string phrases get widened to load-bearing-substring matches
so future wording tweaks in profile.py do not break tests for
incidental punctuation reasons."
```

---

## Task 3: Update `gap_classifier.py` if (and only if) it references `joe-profile.md`

**Files:**

- Read-and-decide: `projects/monolith/knowledge/gap_classifier.py`

### Step 1 — Verify no current consumers of `joe-profile.md`

`grep -rn "joe-profile\.md\|joe_profile" projects/monolith --include="*.py" 2>/dev/null` — confirm zero hits. The .md file was added in PR #2371 but no code reads it; only this morning's ad-hoc subagent dispatch read it (subagent prompts, not committed code).

If hits exist (unexpected), STOP and report — the plan needs to account for them. Otherwise, **this task is a no-op**; skip to Task 4. Do NOT create a placeholder commit.

---

## Task 4: Delete `projects/monolith/knowledge/joe-profile.md`

**Files:**

- Delete: `projects/monolith/knowledge/joe-profile.md`

### Step 1 — Confirm no code consumers (re-check)

`grep -rn "joe-profile\.md\|joe_profile" projects/monolith --include="*.py" 2>/dev/null` — must return zero. (Same check as Task 3, run again as belt-and-braces.) If non-zero, STOP.

### Step 2 — Delete the file

```bash
git rm projects/monolith/knowledge/joe-profile.md
```

### Step 3 — Self-review

- `ls projects/monolith/knowledge/joe-profile.md 2>&1` → `No such file or directory`
- `git status --short` → shows `D projects/monolith/knowledge/joe-profile.md` and nothing else (since the only delta in this task is the deletion)
- `grep -rn "joe-profile\.md" projects/monolith` → zero matches anywhere in the repo

### Step 4 — Commit

```bash
git rm --cached projects/monolith/knowledge/joe-profile.md 2>/dev/null || true  # belt-and-braces; the git rm above already staged it
git commit -m "chore(knowledge): delete joe-profile.md superseded by profile.py

joe-profile.md was added this morning via PR #2371 (commit ed8830fa54)
as the single source of truth for Joe's relevance + privacy rubric.
Task 1 of this PR migrated all load-bearing content into
projects/monolith/knowledge/profile.py as typed Python constants
(IDENTITY, RELEVANCE_KEEP, RELEVANCE_SKIP, VISIBILITY_CRITERIA,
PRIVATE_CATEGORIES, ASYMMETRIC_ERROR_PREFERENCE).

The markdown file is deleted in the same PR so there is never a
window where two sources are live. Future agents and classifiers
import from knowledge.profile; markdown-rubric-as-config is gone."
```

---

## Task 5: `answer_gap` writes `visibility: private` on new atoms

**Files:**

- Modify: `projects/monolith/knowledge/gaps.py` (`answer_gap` atom-creation block, around line 906-914)
- Modify: `projects/monolith/knowledge/gap_lifecycle_test.py` (2 new tests in the `test_answer_gap_*` cluster)

### Step 1 — Add `visibility` to the frontmatter dict

In `projects/monolith/knowledge/gaps.py`, find the `answer_gap` function's atom-creation block (search for `fm = {` inside `def answer_gap`). The current code:

```python
fm = {
    "id": note_id,
    "title": gap.term,
    "type": "atom",
    "source_tier": "personal",
}
```

Change to:

```python
fm = {
    "id": note_id,
    "title": gap.term,
    "type": "atom",
    "source_tier": "personal",
    # User-typed gap answers default private. Joe writes about his own
    # context (people, projects, personal decisions) more often than
    # generic knowledge; the visibility-review queue flips the minority
    # that should be public. Public-default would be riskier here because
    # the body is user-supplied free-form text. See profile.py's
    # ASYMMETRIC_ERROR_PREFERENCE: 'private' is the right error direction.
    "visibility": "private",
}
```

That's the entire change to `gaps.py` for this task — one field added.

### Step 2 — Add two tests

In `projects/monolith/knowledge/gap_lifecycle_test.py`, locate the existing `test_answer_gap_*` cluster (line 599+). Add the two tests below at the END of the cluster (after `test_answer_gap_lowercase_tombstone_treated_as_real_answer`, which yesterday's PR placed last). Match the existing style — bare functions, `(session, tmp_path)` signature:

```python
def test_answer_gap_atom_carries_visibility_private_by_default(session, tmp_path):
    """User-typed gap answers default to visibility: private.

    Rationale (see gaps.answer_gap atom-creation block): gap answers are
    typically Joe writing about his own context (people, projects, personal
    decisions). The visibility-review queue can flip to public for the
    minority that are generic professional knowledge. Public-default
    would be riskier because the body is user-supplied free-form text.

    Pins the closing of the null-visibility regrowth path: before this
    commit, answer_gap-produced atoms had no visibility field at all,
    and they accumulated in the review queue indefinitely (74% of the
    2026-05-28 audit's null-vis pile).
    """
    gap_id = _seed_reviewable_gap(session, term="some-term")
    answer_gap(session, gap_id, "A perfectly normal answer about the term.", tmp_path)
    atom = (tmp_path / "_processed" / "some-term.md").read_text()
    assert "\nvisibility: private\n" in atom, (
        f"atom must carry 'visibility: private' in frontmatter; got:\n{atom[:400]}"
    )


def test_answer_gap_tombstone_path_does_not_produce_atom(session, tmp_path):
    """Regression guard for the interaction between today's two changes
    to answer_gap. The Tombstone-prefix short-circuit (shipped in PR #2370)
    must continue to skip atom creation entirely; no atom file means no
    visibility field to assert about. If the visibility insertion broke
    this short-circuit, the gardener would have to re-classify Tombstone
    answers, and Joe's 'this gap doesn't deserve an atom' convention
    silently regresses.
    """
    gap_id = _seed_reviewable_gap(session, term="tomb-term")
    answer_gap(session, gap_id, "Tombstone - not worth a content atom", tmp_path)
    assert not (tmp_path / "_processed" / "tomb-term.md").exists()
```

`_seed_reviewable_gap` accepts `term=...` as a keyword (verified: line 584, signature `def _seed_reviewable_gap(session: Session, *, term: str = "Linkerd mTLS") -> int`). `answer_gap` and `Gap` are already imported at module top (verified: lines 18 and 25).

### Step 3 — Self-review

- `grep -n '"visibility": "private"' projects/monolith/knowledge/gaps.py` → exactly 1 match, inside `answer_gap`'s `fm` dict
- `grep -c "def test_answer_gap_atom_carries_visibility_private_by_default\|def test_answer_gap_tombstone_path_does_not_produce_atom" projects/monolith/knowledge/gap_lifecycle_test.py` → `2`
- `git diff --stat` → 2 files: `gaps.py`, `gap_lifecycle_test.py`. No BUILD edit.
- Mentally walk the happy-path test: `_seed_reviewable_gap(session, term="some-term")` creates a `Gap(term="some-term", state="in_review", ...)`. `answer_gap` runs all validation, isn't tombstone-prefixed, builds `fm` dict (now with `visibility: "private"`), `yaml.dump`s it, writes to `_processed/some-term.md`. The asserted substring `"\nvisibility: private\n"` appears because `yaml.dump` emits each key on its own line followed by a newline.

### Step 4 — Commit

```bash
git add projects/monolith/knowledge/gaps.py projects/monolith/knowledge/gap_lifecycle_test.py
git commit -m "fix(knowledge): answer_gap writes visibility: private on new atoms

Before this commit, answer_gap built a 4-field frontmatter dict
({id, title, type, source_tier}) and never set 'visibility:'. Every
user-typed gap answer produced a null-visibility atom that
accumulated in the visibility-review queue indefinitely. Today's
2026-05-28 audit found 3225 null-visibility atoms in _processed/
(74% of the corpus); this writer was one of the regrowth sources.

Defaults to 'private' because gap answers are typically Joe writing
about his own context (people, projects, personal decisions), not
generic knowledge. The visibility-review queue can still flip
specific atoms to public; what we eliminate here is the silent leak
where atoms were created without any visibility classification.

The gardener already sets visibility via VISIBILITY_CRITERIA in its
distill prompts (gardener.py:161, 197 -- unchanged here, still
re-exports from profile.py); the raw-reconciler creates type:raw
shadow notes that aren't user-facing and don't need a visibility
field. answer_gap was the remaining leak.

Two new tests: one pins the new default; one regression-guards the
interaction with the Tombstone short-circuit (shipped earlier in
PR #2370) so the no-atom path still works."
```

---

## Task 6: Push, PR, end-of-PR review, merge, verify

### Step 1 — Confirm branch state

```bash
cd /tmp/claude-worktrees/visibility-defaults
git status              # working tree clean
git log --oneline origin/main..HEAD
```

Expected: 5 commits ahead of main (plan-doc commit from this skill + 4 implementation commits from Tasks 1, 2, 4, 5; Task 3 was a no-op).

### Step 2 — Push and open the PR

```bash
git push -u origin feat/visibility-defaults-from-profile

gh pr create --title "feat(knowledge): profile.py replaces joe-profile.md + answer_gap writes visibility" --body "$(cat <<'EOF'
## Summary

Lane B from today's visibility-classification session. Two coherent fixes that close the null-visibility regrowth path and consolidate the rubric onto a single, typed, importable Python source of truth.

1. **`projects/monolith/knowledge/profile.py`** - new module carrying \`IDENTITY\`, \`RELEVANCE_KEEP\`, \`RELEVANCE_SKIP\`, \`VISIBILITY_CRITERIA\`, \`PRIVATE_CATEGORIES\`, \`ASYMMETRIC_ERROR_PREFERENCE\`, plus \`PROFILE_VERSION\` / \`PROFILE_UPDATED\` audit-trail constants. Replaces \`joe-profile.md\` (PR #2371) - markdown rubric is deleted in the same PR so there is never a window where two sources are live.
2. **\`visibility.py\` re-exports \`VISIBILITY_CRITERIA\`** from \`profile.py\` so existing consumers (\`gardener.py:161, 197\`, \`gardener_distill_test.py:30\`) keep working unchanged.
3. **\`answer_gap\` writes \`visibility: private\`** on the atoms it creates. This closes the last null-visibility regrowth path identified in this morning's audit (3225 null-visibility atoms, 74% of the corpus; \`answer_gap\` was one of the writers leaving \`visibility:\` unset).

## Why now

The 2026-05-28 vault audit classified 3225 null-visibility atoms by hand via 5 parallel subagents reading \`joe-profile.md\`. Without patching the writers, that null pile regrows from \`answer_gap\` alone. The criteria also lived twice (\`.md\` rubric + \`VISIBILITY_CRITERIA\` Python literal) - the visibility module's own docstring warned against this exact failure mode.

## Test plan

- [ ] CI green on the branch (\`gh pr checks --watch\`)
- [ ] After merge + monolith rollout, answering a real gap with a non-Tombstone answer produces \`_processed/<slug>.md\` with \`visibility: private\` in the frontmatter
- [ ] Confirm the gardener's next decomposition cycle still produces atoms with visibility (i.e., the re-export refactor did not break the gardener distill prompts)
- [ ] Confirm \`monolith-list-gaps\` and the visibility-review-queue endpoint still behave normally

## Out of scope

- Patching the gardener (already correct - it consumes \`VISIBILITY_CRITERIA\` via the existing import path, which still resolves).
- Defaulting visibility on \`type: raw\` shadow notes (not user-facing on any public surface).
- A new visibility-classifier helper module (the existing \`visibility.py\` plus the new \`profile.py\` already cover what code consumes).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

### Step 3 — Watch CI

```bash
gh pr checks <number> --watch
```

If red, read the failure via `mcp__buildbuddy__get_invocation` (selector: `commitSha`) → `get_target` → `get_log`. Quote the actual assertion error verbatim before hypothesising a cause.

### Step 4 — One end-of-PR code review (per project CLAUDE.md)

Once CI is green, run ONE comprehensive code review against the full diff (NOT per-task). Either dispatch `superpowers:code-reviewer` for an independent agent, or walk the diff manually via `git diff origin/main...HEAD`. Confirm:

- The new `profile.py` constants match the structure pinned by its tests (every row has the keys the test asserts).
- `visibility.py` no longer carries a literal `VISIBILITY_CRITERIA` block - only the re-export.
- The `answer_gap` change is exactly one added field, inside the `fm = {...}` dict, with the comment above explaining the default.
- `joe-profile.md` is deleted (not edited; not partially migrated; gone).
- No accidental changes to other gap verbs (`reject_gap`, `verify_gap`, `reopen_gap`, `approve_gap`).
- No `# nosemgrep`, no `--no-verify`.
- Conventional Commits format on all 4 implementation commits.

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

1. **Visibility default on `answer_gap`** - via UI: open `/private/review?tab=gaps&mode=pending`, find an internal/hybrid gap, answer it with normal prose (not "Tombstone"), submit. Inspect the produced `_processed/<slug>.md` (via vault or `monolith-get-note`) - frontmatter must include `visibility: private`.

2. **Gardener still consumes `VISIBILITY_CRITERIA`** - wait for the next gardener tick (visible in `kubernetes-mcp-pods-log` for the monolith pod), or trigger via `monolith-monolith-agent-trigger-job(name="knowledge.garden")`. Confirm the LLM still produces atoms with `visibility:` set (the re-export should be transparent).

3. **`joe-profile.md` is gone** - on the live monolith pod's filesystem, `ls projects/monolith/knowledge/joe-profile.md` returns `No such file or directory`. (This will be true automatically since the image was rebuilt from main.)

---

## Out of scope (do not let any task expand into these)

- **Migrating gap_classifier.py's `_CLASSIFIER_PROMPT` to consume `RELEVANCE_KEEP` / `RELEVANCE_SKIP`** from `profile.py`. Currently the classifier prompt has its own rubric (external/internal/hybrid/parked) which is a smaller decision space than the KEEP/SKIP tables. Reconciling them is a separate calibration question; out of scope here.
- **A startup hook that asserts `profile.py`'s constants are well-formed at import time**. The tests do that.
- **Bumping `PROFILE_VERSION` to `"3"` after the initial `"2"`**. We're already migrating from v1 (the markdown file) to v2 (the Python module). The version stays at `"2"` for this PR; bumps happen on substantive content edits in future PRs.
- **Backfilling the local-filesystem null-visibility atoms** that haven't sync'd from the monolith pod's vault yet via Obsidian Sync. They'll resolve on the next sync cycle.

## Definition of done

- [ ] All five implementation commits land (Tasks 1, 2, 4, 5 — Task 3 is a no-op verification step).
- [ ] All new tests + existing tests green on the branch's CI run.
- [ ] One end-of-PR code review completed against the full diff.
- [ ] PR rebase-merged to main.
- [ ] After merge + rollout: next `answer_gap` produces an atom with `visibility: private`; next gardener tick still produces atoms with visibility; `joe-profile.md` is gone from the image.
