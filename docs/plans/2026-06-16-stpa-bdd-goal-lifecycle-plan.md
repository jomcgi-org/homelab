# Plan: STPA + future-BDD + `/goal` lifecycle

Status: in progress
Date: 2026-06-16
Owner: Joe

## Goal

Stand up an AI-development lifecycle that runs as `/goal "<feature>"` and drives a
feature from design to safety assessment, compacting context as it goes:

1. plan / iterate on design
2. ADR the rationale
3. merge failing BDD tests (executable spec, visibly red, non-gating)
4. complete the feature (specs go green, promote them)
5. merge an STPA (logical + physical) assessment or update

The lifecycle composes existing skills (`brainstorming`, `writing-plans`, `adr`,
`subagent-driven-development`) plus two new pieces built here.

## Design (settled)

- **`/goal` is a skill** (agent orchestration), not a CI workflow.
- **`bdd-future-feature` is a CI workflow** (a `buildbuddy.yaml` action) that runs
  `future`-tagged specs as a **non-required** check, so red specs can merge.
- **`stpa` is a skill** (port of the loom STPA skill), deterministic JSON to jq
  render to PR-on-green.
- **Compaction principle:** durable state lives in artifacts (plan doc, ADR,
  committed specs, `STPA.md`, the PR), never in context. Heavy work is delegated
  to subagents (fresh context each). A compaction or fresh session loses nothing.
- **STPA placement:** colocated, one `STPA.md` per system
  (`projects/monolith/STPA.md`, `bazel/STPA.md`, ...). Logical and physical are
  two sections inside that one file, tagged via a `layer` field in the JSON.
- **Merge cadence:** `/goal` auto-merges the low-risk phases on green (plan doc,
  ADR, failing-BDD specs, STPA refresh: all docs/test-only, rebase auto-merge) but
  **pauses at the phase-4 implementation PR** for the repo-mandated code review,
  then resumes for STPA.
- **Repo adaptations from loom:** rebase-merge only (no squash), Conventional
  Commits enforced by the commit-msg hook, `format` hook.

## Tasks

### Task 1: future-BDD lane

- `projects/monolith/bdd_test.bzl`: add `future = False`; when set, append a
  `future` tag (a future spec is a normal `bdd_test(future = True, ...)`).
- `buildbuddy.yaml`:
  - Gating `Test` action: exclude `future` from the `//...` run
    (`--test_tag_filters=-external,-future`) so red specs do not gate.
  - New non-required action `BDD future features`: query `future`-tagged tests;
    none -> green no-op; some -> run them and report honestly. Red while a feature
    is mid-build is the expected, non-blocking state; green = ready to promote
    (drop the `future` tag so the spec rejoins gating CI).
- Note: "non-required" is a GitHub branch-protection setting (the action reports a
  status; we simply do not list it as required). Documented in the action comment.

### Task 2: `stpa` skill

- `.claude/skills/stpa/SKILL.md`: port the loom skill with:
  - `system` arg = the directory to analyze; output `<system>/STPA.md`.
  - JSON schema: nodes / control_actions / ucas carry `layer: logical|physical`.
  - Renderer (BLOCK A) emits one doc with two "Control structure" subsections
    (logical, physical); a system may omit a layer.
  - PR block (BLOCK B): `gh pr merge --rebase` (squash disabled here);
    conventional `docs(stpa): ...` title; keep the commit-msg hook (no blanket
    `--no-verify`).
  - Seed target: `projects/monolith/` first.

### Task 3: `/goal` orchestrator skill

- `.claude/skills/goal/SKILL.md`: the 5-phase conductor.
  - Each phase: produce artifact -> land PR -> gate on CI green.
  - Resumable: read a small state marker; continue from first incomplete phase.
  - Delegate heavy work to subagents (compaction).
  - Auto-merge (rebase) phases 1, 2, 3, 5; pause at phase 4 for human review.
  - Invokes the `stpa` skill against whichever system(s) the feature touched.

## Verification

No local test loop. Commit, push the branch, open a PR, watch BuildBuddy CI:
- Format check (auto-formats on PR branch).
- `Test` (`bazel test //...`): the new `bdd_test` param is backward compatible;
  the `-future` filter change must not break the existing `bdd`-tagged run.
- The new `BDD future features` action is informational; with no `future` specs
  yet it must no-op green.
- SKILL.md files are not tested; verified by review.

## Out of scope (follow-ups)

- STPA freshness check (non-required CI: subtree changed but `STPA.md` cites an old
  commit). Nudge, not a gate.
- A generated index of all `**/STPA.md`.
- An ADR recording this lifecycle as a process decision.
