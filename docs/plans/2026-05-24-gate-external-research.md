# Gate External Research Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. This repo has **no local test loop** (see "Test execution policy" below) — every "verify it passes" check happens on the pushed branch's CI run, not on your workstation. Self-review before each commit; one end-of-PR code review against the full diff (per project CLAUDE.md), not per-task reviewers.

**Goal:** Route `external` knowledge gaps through the existing review-queue UI so Sonnet web research only runs on gaps Joe has explicitly approved.

**Architecture:** Reuse the existing `in_review` state and the existing pending-review screen at `/private/review?tab=gaps&mode=pending`. The classifier prompt changes so `external` rows land in `in_review` alongside `internal`/`hybrid`. A new `approve_gap` service verb flips `in_review` → `classified` for external gaps; the daily research cron's sweep query (`gap_class=='external' AND state=='classified'`) picks them up unchanged. The frontend's existing `yes`/`no` decision dispatch grows one new branch keyed on `item.gap_class`. A one-line data migration retroactively gates the ~210 `external+classified` rows already queued.

**Tech Stack:** Python 3 (FastAPI, SQLModel), Postgres (Atlas-managed migrations, schema `knowledge`), pytest (SQLite fixtures via `create_all`), Svelte 5 (runes), Bazel + BuildBuddy CI (no local test runner).

---

## Test execution policy (read this before Task 1)

This repo's BuildBuddy `workflows` pool has no darwin runners and the linux fallback is too slow/flaky to be the inner loop. **Do not** run `bazel test`, `pytest`, `pnpm test`, or any other test runner from the workstation. Instead:

- Write tests in the same commit as the implementation they cover.
- After each task's commit, push the branch and watch the PR's CI run via `gh pr checks <number> --watch`.
- If CI is red, read the failure via `mcp__buildbuddy__get_invocation` (selector: `commitSha`) → `get_target` → `get_log`. Quote the actual assertion error verbatim before hypothesising a cause.
- The "expected output" lines below describe what CI should show, not what to run locally.

Every commit message follows Conventional Commits (`feat(knowledge): …`, `fix(knowledge): …`, etc.). Branch is already created at `feat/gate-external-research` in the worktree at `/tmp/claude-worktrees/gate-external-research`.

---

## Repo-specific gotchas the executor must respect

1. **`# gazelle:exclude knowledge`** — `projects/monolith/BUILD` is hand-maintained for everything under `projects/monolith/knowledge/`. New test files require an explicit `py_test` entry. (For this plan all new tests go into existing test files, so no BUILD edits are required. Verify before each commit.)
2. **SQLite test fixtures use `create_all`, not migrations.** The new SQL migration in Task 1 will not run in unit tests. Tests must seed rows in their desired starting state directly. Mirror existing CHECK constraints in `__table_args__` if a new one is needed (this plan adds no new constraints).
3. **No `psycopg3` text-cast quirks for this PR** — we're only adding a single-column UPDATE inside SQLAlchemy ORM, not raw `text(...)` SQL.
4. **The MCP function name controls the exposed tool name.** A Python `@mcp.tool` function named `approve_research_gap` is reachable as `monolith-approve-research-gap`. Match the function name to the spec.

---

## Task 1: Data migration retroactively gates the existing backlog

**Files:**

- Create: `projects/monolith/chart/migrations/20260524000000_gate_external_research.sql`
- Modify: `projects/monolith/chart/migrations/atlas.sum` (Atlas auto-regenerates; see Step 4)

**Step 1: Write the migration**

Create `projects/monolith/chart/migrations/20260524000000_gate_external_research.sql` with this exact body:

```sql
-- Gate external research behind the review queue.
--
-- Pairs with the classifier-prompt change (CLASSIFIER_VERSION opus-4-7@v2)
-- that routes external -> in_review instead of straight to classified.
-- This migration retroactively gates the ~210 external+classified rows
-- already queued for the daily research cron, so they appear in the
-- pending review-queue UI and only drain after explicit approval via
-- POST /api/knowledge/gaps/{id}/approve (which flips in_review back to
-- classified, where the existing _sweep_and_select_candidates picks them
-- up unchanged).
--
-- Idempotent: the narrow WHERE clause makes re-applying a no-op once the
-- new classifier is the only writer of external rows, because the new
-- classifier never produces external+classified.
--
-- No CHECK-constraint change needed: 'in_review' has been a valid state
-- since 20260425040000_knowledge_gaps_state_check_widen.sql.
UPDATE knowledge.gaps
   SET state = 'in_review'
 WHERE deleted_at IS NULL
   AND gap_class  = 'external'
   AND state      = 'classified';
```

**Step 2: Sanity-check timestamp ordering**

Run: `ls projects/monolith/chart/migrations/ | tail -5`

Expected: the new `20260524000000_gate_external_research.sql` is the last entry (alphabetically, which Atlas treats as chronologically). If a same-day migration already exists with a later timestamp, bump the new file's timestamp to `20260524010000` (or higher) to stay last.

**Step 3: Regenerate `atlas.sum`**

Atlas requires `atlas.sum` to checksum every migration file. If `atlas` is on PATH (check with `command -v atlas`):

Run: `atlas migrate hash --dir file://projects/monolith/chart/migrations`

If atlas isn't available locally, leave `atlas.sum` alone — CI will fail with a "checksum mismatch" message that includes the line to add. Add it in a follow-up commit before pushing again. (Do not hand-compute the SHA; let atlas write it.)

**Step 4: Self-review**

Read the file back. Confirm:

- Timestamp is the largest in the directory.
- `deleted_at IS NULL` is present (we never resurrect soft-deleted rows).
- The WHERE clause matches `gap_class='external' AND state='classified'` exactly.
- The comment names the paired classifier change so the next person reading `git blame` knows why this exists.

**Step 5: Commit**

```bash
git add projects/monolith/chart/migrations/20260524000000_gate_external_research.sql projects/monolith/chart/migrations/atlas.sum
git commit -m "feat(knowledge): gate existing external+classified backlog behind review queue"
```

CI expectation on push: this migration alone (without the classifier change in Task 2) would cause new external gaps from the still-old classifier to drift straight back to `classified`. That's tolerable for one commit's worth of CI but ship Task 2 before merging this PR. Do not merge after only Task 1.

---

## Task 2: Classifier routes external to `in_review`, version bump

**Files:**

- Modify: `projects/monolith/knowledge/gap_classifier.py:29` (version constant)
- Modify: `projects/monolith/knowledge/gap_classifier.py:67-74` (status-rule block in `_CLASSIFIER_PROMPT`)
- Modify: `projects/monolith/knowledge/gap_classifier_test.py:190-220` (extend or add drift detector)

**Step 1: Bump `CLASSIFIER_VERSION`**

In `projects/monolith/knowledge/gap_classifier.py`, change line 29 from:

```python
CLASSIFIER_VERSION = "opus-4-7@v1"
```

to:

```python
CLASSIFIER_VERSION = "opus-4-7@v2"
```

This propagates into every stub's `classifier_version` frontmatter on the next classify tick, which lets `git blame` / DB queries discriminate gaps classified under the new routing rule.

**Step 2: Edit the status-rule block in `_CLASSIFIER_PROMPT`**

Replace the current bullet (lines 69-74):

```
      - find: `status: discovered` → replace with the status that matches
        the class you just chose:
          - `status: classified` for `gap_class: external` or `parked`
            (external flows into the research pipeline; parked is terminal)
          - `status: in_review` for `gap_class: internal` or `hybrid`
            (these surface in the user's review queue for them to answer)
```

with:

```
      - find: `status: discovered` → replace with the status that matches
        the class you just chose:
          - `status: in_review` for `gap_class: external`, `internal`, or
            `hybrid` (every gap that could ever flow into the research
            pipeline or need a user answer goes through the review queue
            first — Joe approves external rows from there before any
            tokens are spent)
          - `status: classified` for `gap_class: parked` only (terminal
            — never flows further; classified is the terminal label for
            parked gaps that bypass the review queue)
```

**Step 3: Update the drift-detector test**

In `projects/monolith/knowledge/gap_classifier_test.py`, rename and extend `test_classifier_prompt_routes_internal_and_hybrid_to_in_review` (line 190) to cover external too. Replace the function body with:

```python
def test_classifier_prompt_routes_internal_hybrid_external_to_in_review():
    """Drift detector: internal/hybrid/external must transition to in_review.

    External moved into in_review in CLASSIFIER_VERSION opus-4-7@v2 to
    gate Sonnet web research behind explicit user approval — the review
    queue is the approval surface. Only `parked` should still route to
    status: classified (a terminal label for parked gaps that bypass the
    queue).

    Without this test, regressing the routing for any of the three
    user-actionable classes silently empties the pending review queue
    for that class and (for external) re-enables the unguarded research
    drain that v2 was introduced to stop.
    """
    rendered = _CLASSIFIER_PROMPT.format(
        classifier_version=CLASSIFIER_VERSION,
        stub_list="- /tmp/example.md",
    )

    # Both terminal-ish statuses must be reachable from the prompt:
    # `in_review` (the user-actionable lane) and `classified` (the
    # parked-only escape hatch).
    assert "status: in_review" in rendered, (
        "prompt must produce status: in_review for external/internal/hybrid"
    )
    assert "status: classified" in rendered, (
        "prompt must still produce status: classified for parked"
    )

    # The in_review branch must explicitly name all three user-actionable
    # classes within a tight window so the model can't reasonably misroute.
    # 240 chars covers the bullet line plus its parenthetical.
    after_in_review = rendered.split("status: in_review", 1)[1][:240]
    for cls in ("external", "internal", "hybrid"):
        assert cls in after_in_review, (
            f"in_review branch must name the {cls} class explicitly"
        )

    # The classified branch must name `parked` and must NOT name external
    # (regression guard against the v1 routing reappearing).
    after_classified = rendered.split("status: classified", 1)[1][:200]
    assert "parked" in after_classified, (
        "classified branch must name parked explicitly"
    )
    assert "external" not in after_classified, (
        "classified branch must NOT mention external — v2 routes external "
        "through in_review for approval gating"
    )
```

**Step 4: Self-review**

Read the prompt change back end-to-end. Confirm:

- The bullet under "find: `status: discovered` →" is the only edit (no body or wrapping changed).
- The classifier rubric block (the four classes) was NOT edited — only the routing.
- The example in the parenthetical mentions the approval gate so future readers understand why external is in this list.
- `CLASSIFIER_VERSION` is `opus-4-7@v2`.

**Step 5: Commit**

```bash
git add projects/monolith/knowledge/gap_classifier.py projects/monolith/knowledge/gap_classifier_test.py
git commit -m "feat(knowledge): classifier v2 routes external gaps to in_review

External gaps now land in the pending review queue alongside
internal/hybrid so Joe can approve them before the daily research
cron spends Sonnet tokens. Only 'parked' still routes directly to
status: classified (terminal). Pairs with the data migration that
retroactively gates the existing backlog."
```

CI expectation on push: `test_classifier_prompt_routes_internal_hybrid_external_to_in_review` passes; the old test name no longer exists, so no stale reference will compile.

---

## Task 3: `approve_gap` service function + tests

**Files:**

- Modify: `projects/monolith/knowledge/gaps.py` (add `approve_gap` near the `reject_gap` / `reopen_gap` block around line 650-728)
- Modify: `projects/monolith/knowledge/gap_review_endpoints_test.py` (add tests next to the existing reject_gap tests)

**Step 1: Locate the insertion point in `gaps.py`**

Open `projects/monolith/knowledge/gaps.py`. Find `def reject_gap(` (around line 650). Insert `approve_gap` immediately after `def reopen_gap(` (which ends around line 727), before `def answer_gap(` (around line 730). Keeping the verbs together makes the file's pending/audit verb cluster scannable.

**Step 2: Add the `approve_gap` function**

Insert this exact code:

```python
def approve_gap(session: Session, gap_id: int) -> dict:
    """Approve an external gap for auto-research: in_review -> classified.

    The classifier (CLASSIFIER_VERSION opus-4-7@v2 onward) routes external
    gaps into ``state='in_review'`` so the user can explicitly opt into
    spending Sonnet web-research tokens on each one. Flipping back to
    ``state='classified'`` re-arms the daily research cron's
    ``_sweep_and_select_candidates`` sweep, which selects on
    ``gap_class='external' AND state='classified'`` -- so this function
    is the *only* path back into the research pipeline after the v2
    cutover.

    Sets ``human_verified=True`` because approval is an explicit user
    action on the gap, mirroring :func:`reject_gap` / :func:`answer_gap`.
    Does not touch the stub file: the reconciler will project the new
    state on its next tick.

    Raises:
        ValueError: if ``gap_id`` is unknown, the gap is not in
            ``state='in_review'``, or the gap's ``gap_class`` is not
            ``'external'`` (only externals consume the research pipeline;
            approving an internal/hybrid would be a no-op at best and a
            wrong-pipeline routing at worst).
    """
    gap = _get_gap_or_raise(session, gap_id)
    if gap.state != "in_review":
        raise ValueError(
            f"Gap id={gap_id} is in state={gap.state!r}, expected 'in_review'"
        )
    if gap.gap_class != "external":
        raise ValueError(
            f"Gap id={gap_id} has gap_class={gap.gap_class!r}, expected 'external'"
        )

    gap.state = "classified"
    gap.human_verified = True
    session.commit()
    session.refresh(gap)
    logger.info(
        "gaps.approve_gap: approved gap_id=%d term=%r for research", gap_id, gap.term
    )
    return _gap_to_dict(gap, session=session)
```

**Step 3: Write the failing tests**

Open `projects/monolith/knowledge/gap_review_endpoints_test.py`. Find the test class or section that covers `reject_gap` (search for `def test_reject` or `class TestRejectGap`). Add a sibling test block (class or contiguous test functions, matching whatever style the surrounding tests use) covering:

```python
# ---------------------------------------------------------------------------
# approve_gap — gate external research behind explicit user approval.
# ---------------------------------------------------------------------------


def test_approve_gap_flips_in_review_external_to_classified(session):
    """Happy path: in_review + external -> classified, human_verified=True."""
    from knowledge.gaps import approve_gap

    gap = _make_gap(session, term="linkerd-mtls", state="in_review",
                    gap_class="external")
    result = approve_gap(session, gap.id)
    session.refresh(gap)
    assert gap.state == "classified"
    assert gap.human_verified is True
    assert result["state"] == "classified"


def test_approve_gap_rejects_unknown_id(session):
    """Unknown gap_id -> ValueError('Gap not found ...')."""
    from knowledge.gaps import approve_gap

    with pytest.raises(ValueError, match="Gap not found"):
        approve_gap(session, 999_999)


def test_approve_gap_rejects_wrong_state(session):
    """state != 'in_review' -> ValueError("expected 'in_review'")."""
    from knowledge.gaps import approve_gap

    gap = _make_gap(session, term="x", state="classified", gap_class="external")
    with pytest.raises(ValueError, match="expected 'in_review'"):
        approve_gap(session, gap.id)


def test_approve_gap_rejects_internal_class(session):
    """gap_class != 'external' -> ValueError("expected 'external'")."""
    from knowledge.gaps import approve_gap

    gap = _make_gap(session, term="my-therapist", state="in_review",
                    gap_class="internal")
    with pytest.raises(ValueError, match="expected 'external'"):
        approve_gap(session, gap.id)


def test_approve_gap_rejects_hybrid_class(session):
    """Hybrid is user-answerable, not auto-researchable; reject approval."""
    from knowledge.gaps import approve_gap

    gap = _make_gap(session, term="my-neovim-config", state="in_review",
                    gap_class="hybrid")
    with pytest.raises(ValueError, match="expected 'external'"):
        approve_gap(session, gap.id)


def test_approve_gap_idempotency_second_call_rejected(session):
    """Once approved (state=classified), a second approve raises the
    wrong-state error — the gap must be re-routed through the cron, not
    re-approved.
    """
    from knowledge.gaps import approve_gap

    gap = _make_gap(session, term="x", state="in_review", gap_class="external")
    approve_gap(session, gap.id)
    with pytest.raises(ValueError, match="expected 'in_review'"):
        approve_gap(session, gap.id)
```

**Important: `_make_gap` signature.** Open the same test file and confirm the existing `_make_gap` helper's signature (search for `def _make_gap`). Match the kwargs in the new tests to whatever it actually accepts — the test file at HEAD already uses `term=`, `state=`, `gap_class=`, plus a `source_fk` if required. If `source_fk` is mandatory (it is in `gap_api_test.py:78`), thread a fixture-created source row through the same way the surrounding tests do.

**Step 4: Self-review**

- Confirm `approve_gap` is exported alongside the other verbs if `gaps.py` has an `__all__` (grep for it; if absent, no action needed — Python re-exports by default).
- Confirm the validation order: state check first, then class check. This matches `reject_gap`'s structure (state check first) and yields the most useful error message for a UI that's already known to be on a pending gap.
- Confirm `_gap_to_dict(gap, session=session)` is the same return shape as `reject_gap` (line 677) — the frontend's `decide` action ignores the body, but mirroring the shape keeps the API surface uniform.

**Step 5: Commit**

```bash
git add projects/monolith/knowledge/gaps.py projects/monolith/knowledge/gap_review_endpoints_test.py
git commit -m "feat(knowledge): add approve_gap verb (in_review external -> classified)

Approves an external gap for auto-research by flipping in_review back
to classified, which re-arms the daily research cron's existing
sweep. Sets human_verified=True. Rejects unknown ids, wrong states,
and non-external gap_classes. Tests mirror the reject_gap shape."
```

CI expectation on push: six new tests pass under `knowledge_gap_review_endpoints_test` in the BuildBuddy invocation.

---

## Task 4: HTTP route + MCP tool

**Files:**

- Modify: `projects/monolith/knowledge/router.py` (add `POST /gaps/{gap_id}/approve` next to `reject_gap_endpoint` around line 722)
- Modify: `projects/monolith/knowledge/mcp.py` (add `approve_research_gap` next to `answer_gap` around line 392)
- Modify: `projects/monolith/knowledge/router_test.py` and/or `projects/monolith/knowledge/mcp_gap_test.py` (add endpoint + tool tests in whichever file currently houses the analogous reject tests — check both)

**Step 1: Add the import**

In `projects/monolith/knowledge/router.py`, find the existing import block that pulls `answer_gap, reject_gap, reopen_gap, verify_gap` from `knowledge.gaps` (around line 33). Add `approve_gap` to that import list, keeping alphabetical order if the existing list is sorted (it is).

**Step 2: Add the route**

Insert this after `reject_gap_endpoint` (which ends around line 732) and before `verify_gap_endpoint`:

```python
@router.post("/gaps/{gap_id}/approve")
def approve_gap_endpoint(
    gap_id: int,
    session: Session = Depends(get_session),
) -> dict:
    """Approve an external gap for auto-research.

    Transitions ``in_review`` → ``classified`` so the daily research
    cron's sweep picks it up. Only valid for ``gap_class='external'``.
    """
    try:
        return approve_gap(session, gap_id)
    except ValueError as exc:
        raise _map_gap_error(exc) from exc
```

`_map_gap_error` already handles both error strings (`"Gap not found"` → 404 and any `\bexpected\b` message → 409), so no mapper change is needed. Confirm by re-reading `_map_gap_error` (router.py:678) before committing.

**Step 3: Add the MCP tool**

In `projects/monolith/knowledge/mcp.py`, add to the existing import block at the top (line 23):

```python
from knowledge.gaps import approve_gap as _approve_gap
```

Then add this tool definition next to `answer_gap` (after the function at line 393, before any trailing block of unrelated helpers):

```python
@mcp.tool
async def approve_research_gap(gap_id: int) -> dict:
    """Approve an external gap for auto-research; the daily research
    cron will pick it up on its next tick.

    Use this from the pending review queue when you decide an external
    gap is worth Sonnet web-research tokens. Internal/hybrid gaps must
    be answered via ``answer_gap`` instead — approval rejects them.

    Args:
        gap_id: The id of a gap currently in ``state='in_review'`` with
            ``gap_class='external'``.
    """
    with Session(get_engine()) as session:
        try:
            return _approve_gap(session, gap_id)
        except ValueError as exc:
            return {"error": str(exc)}
```

The exposed MCP tool ID will be `monolith-approve-research-gap` (the function name minus underscores, prefixed with the server name).

**Step 4: Add endpoint + tool tests**

Add to `projects/monolith/knowledge/router_test.py` (use the same fixtures and styling as the existing `test_reject_gap_*` endpoint tests — grep the file to find them, then mirror the shape):

```python
def test_approve_gap_endpoint_happy_path(client, session):
    """POST /api/knowledge/gaps/{id}/approve flips in_review -> classified."""
    gap = _make_gap(session, term="rust-ownership", state="in_review",
                    gap_class="external")
    r = client.post(f"/api/knowledge/gaps/{gap.id}/approve")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "classified"
    assert body["human_verified"] is True


def test_approve_gap_endpoint_unknown_id_404(client):
    r = client.post("/api/knowledge/gaps/999999/approve")
    assert r.status_code == 404


def test_approve_gap_endpoint_wrong_state_409(client, session):
    gap = _make_gap(session, term="x", state="classified", gap_class="external")
    r = client.post(f"/api/knowledge/gaps/{gap.id}/approve")
    assert r.status_code == 409


def test_approve_gap_endpoint_wrong_class_409(client, session):
    gap = _make_gap(session, term="my-therapist", state="in_review",
                    gap_class="internal")
    r = client.post(f"/api/knowledge/gaps/{gap.id}/approve")
    assert r.status_code == 409
```

Add to `projects/monolith/knowledge/mcp_gap_test.py` (mirror the existing `answer_gap` test cluster around line 246):

```python
@pytest.mark.asyncio
async def test_approve_research_gap_happy_path(session):
    """approve_research_gap MCP tool flips an external in_review gap."""
    from knowledge.mcp import approve_research_gap

    gap = _make_gap(session, term="merkle-tree", state="in_review",
                    gap_class="external")
    result = await approve_research_gap(gap.id)
    assert result["state"] == "classified"
    assert "error" not in result


@pytest.mark.asyncio
async def test_approve_research_gap_unknown_id_returns_error(session):
    from knowledge.mcp import approve_research_gap

    result = await approve_research_gap(9999)
    assert "error" in result


@pytest.mark.asyncio
async def test_approve_research_gap_wrong_class_returns_error(session):
    from knowledge.mcp import approve_research_gap

    gap = _make_gap(session, term="x", state="in_review", gap_class="internal")
    result = await approve_research_gap(gap.id)
    assert "error" in result
    assert "expected 'external'" in result["error"]
```

If `_make_gap` does not exist in either of those test files (router_test.py / mcp_gap_test.py), either import it from `gap_review_endpoints_test.py` (only if it's a top-level helper, not a fixture) or recreate a minimal local copy keyed to that test file's prevailing pattern. Match what's already there — don't introduce a new shared fixture module.

**Step 5: Self-review**

- Confirm the new route is alphabetically/logically grouped near the other gap verbs (after reject, before verify).
- Confirm the MCP tool docstring tells the agent _when not_ to use it (internal/hybrid → use answer_gap). This is critical: the homelab's autonomous agents will pick the wrong tool by frequency if the description is ambiguous.
- Confirm no BUILD edit is needed — both test files are already registered (check `projects/monolith/BUILD` for `knowledge_router_test` and `knowledge_mcp_gap_test`).

**Step 6: Commit**

```bash
git add projects/monolith/knowledge/router.py projects/monolith/knowledge/mcp.py projects/monolith/knowledge/router_test.py projects/monolith/knowledge/mcp_gap_test.py
git commit -m "feat(knowledge): expose approve_gap via HTTP + MCP

POST /api/knowledge/gaps/{id}/approve (200/404/409 mapped via the
existing _map_gap_error helper) and the matching monolith-approve-
research-gap MCP tool for agent callers. Both delegate to
gaps.approve_gap and round-trip the new wrong-class 409 error."
```

CI expectation on push: new endpoint + MCP tests pass; existing reject/verify/reopen tests untouched.

---

## Task 5: Wire the review-queue UI to the new endpoint

**Files:**

- Modify: `projects/monolith/frontend/src/routes/private/review/+page.svelte` (extend `endpointFor` around lines 72-103)
- Modify: `projects/monolith/frontend/src/lib/private/components/ReviewCard.svelte` (conditional button label around lines 151-157)

**Step 1: Branch `endpointFor` on `item.gap_class` for pending gaps**

In `+page.svelte`, replace the `if (mode === "pending") { ... }` block inside `endpointFor` (lines 74-81) with:

```javascript
if (mode === "pending") {
  // External gaps consume Sonnet web-research tokens, so "yes" must
  // hit the new /approve endpoint (in_review -> classified, which
  // re-arms the daily research cron). Internal/hybrid stay on
  // /verify because there's no auto-pipeline behind them — the
  // user's affirmative is just an acknowledgement.
  const yesPath =
    item.gap_class === "external"
      ? `/api/knowledge/gaps/${item.id}/approve`
      : `/api/knowledge/gaps/${item.id}/verify`;
  return {
    path: action === "yes" ? yesPath : `/api/knowledge/gaps/${item.id}/reject`,
  };
}
```

Do **not** touch the `mode === "audit"` branch. Audit mode is about reopening or agreeing with past terminal decisions, which has nothing to do with the gating change.

**Step 2: Update the button label in `ReviewCard.svelte`**

In `ReviewCard.svelte`, find the actions block (lines 150-168). Replace the pending-gaps "Keep (y)" button label so external gaps read "Approve (y)" instead. Update lines 151-157 to:

```svelte
    {#if mode === "pending"}
      <button class="action action--keep" onclick={() => onDecide("yes")}>
        {#if tab === "gaps"}
          {item.gap_class === "external" ? "Approve (y)" : "Keep (y)"}
        {:else}
          Public (y)
        {/if}
      </button>
      <button class="action action--reject" onclick={() => onDecide("no")}>
        {tab === "gaps" ? "Reject (n)" : "Private (n)"}
      </button>
```

**Step 3: Confirm no other UI references need updating**

- `meta-val` for `gap_class` (ReviewCard.svelte:81-82) already renders the class verbatim — no edit needed; the user sees "external" in the metadata grid alongside the new "Approve (y)" button.
- The keyboard handler (`+page.svelte:328-335`) already routes the `y` key through `handleDecide("yes")`, which now resolves to the approve path for externals. No keybinding work.
- The audit-mode UI is unchanged; ditto the notes tab.

**Step 4: Self-review**

- Grep for any other usage of `/api/knowledge/gaps/.*/verify` to confirm nothing else needs branching: `grep -rn "/verify" projects/monolith/frontend/src/`.
- Look at the optimistic-advance / rollback logic in `handleDecide` (lines 157-219) — confirm it's path-agnostic (it is; only `endpointFor` and the keyboard router know which verb is being fired).
- The `pendingError` banner already surfaces upstream 4xx detail via `readError` in `+page.server.js:57`, so a wrong-class 409 from the new endpoint will display the verbatim FastAPI error message. No additional toast wiring needed.

**Step 5: Commit**

```bash
git add projects/monolith/frontend/src/routes/private/review/+page.svelte projects/monolith/frontend/src/lib/private/components/ReviewCard.svelte
git commit -m "feat(monolith): wire review queue 'yes' to /approve for external gaps

External gaps now consume the daily research cron's tokens only after
explicit user approval. The pending review card relabels 'Keep (y)' as
'Approve (y)' for external rows and POSTs to the new /approve
endpoint; internal/hybrid behaviour is unchanged. Audit mode and the
notes tab are unaffected."
```

CI expectation on push: existing frontend tests (if any cover review/) continue to pass; the change is small and additive.

---

## Task 6: Open the PR and verify end-to-end on CI

**Step 1: Push and create the PR**

If this is the first push:

```bash
git push -u origin feat/gate-external-research
gh pr create --title "feat(knowledge): gate external research behind review queue" --body "$(cat <<'EOF'
## Summary
- Classifier v2 routes external gaps to \`in_review\` so they queue alongside internal/hybrid.
- New \`approve_gap\` verb (HTTP + MCP) flips \`in_review\` → \`classified\` for externals only, re-arming the existing daily research cron sweep.
- Review-queue UI labels external rows "Approve (y)" and POSTs the new endpoint; internal/hybrid stay on \`/verify\`.
- Data migration retroactively gates the ~210 \`external+classified\` rows already queued for auto-research.

## Test plan
- [ ] CI green on the branch (\`gh pr checks --watch\`)
- [ ] After merge + monolith rollout, visit \`/private/review?tab=gaps&mode=pending\` and confirm external gaps render with "Approve (y)" instead of "Keep (y)"
- [ ] Approve one external gap; confirm \`monolith-list-gaps\` reports it as \`state=classified\` and the next daily research-gaps tick picks it up
- [ ] Confirm rejecting an external gap still tombstones the stub as before
- [ ] Confirm internal/hybrid pending behaviour is unchanged (Keep (y) still hits \`/verify\`)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

**Step 2: Watch CI**

```bash
gh pr checks <number> --watch
```

If a check fails, read it via:

```
mcp__buildbuddy__get_invocation (selector: commitSha)
  -> get_target (find the failing target)
  -> get_log
```

Quote the actual assertion or exception message verbatim before proposing a fix. Do not attribute failures to flake/infra without ruling out a real test failure first (per project CLAUDE.md).

**Step 3: End-of-PR code review (per project CLAUDE.md)**

Once CI is green, run **one comprehensive code review** against the full diff (not per-task). Either via `superpowers:requesting-code-review` or by reading `git diff origin/main...HEAD` manually. Confirm:

- The five files touched per task match this plan's "Files" lists.
- No accidental changes to the research handler, the daily cron interval, or any auto-research code path.
- No `bazel test` or `pytest` invocations leaked into commit messages or scripts.

**Step 4: Merge**

This repo only allows rebase-merge:

```bash
gh pr merge --rebase
```

Or for hands-off:

```bash
gh pr merge --auto --rebase
```

**Step 5: Verify the live rollout**

After the monolith pod recycles (ArgoCD auto-syncs ~5-10s after merge; rollout takes ~30-60s):

- Visit `/private/review?tab=gaps&mode=pending` — external gaps should now appear with the "Approve (y)" label.
- Spot-check via the MCP `monolith-list-gaps` tool: `state=classified, gap_class=external` should be empty (or only contain rows freshly approved post-rollout). All previously-classified externals should now show `state=in_review`.
- Optionally trigger a research tick manually via `scheduler` skill or wait 24h for the daily cron — confirm only approved externals get researched.

---

## Out of scope (do not let any task expand into these)

- **Changing the daily cron interval** (currently 86400s in `service.py:45`). Explicit non-goal — revisit after the gate is live and we have a feel for how fast Joe drains the approval queue.
- **DeepSeek / model swap**. Explicit non-goal; orthogonal to the gating change.
- **A net-new `approved_for_research` state.** We deliberately reuse `classified` because it already drives the cron sweep — adding a new state would require a CHECK-constraint widen, reconciler vocabulary update, and drift detectors for negligible benefit.
- **Bulk-rewriting `_researching/<slug>.md` stubs in the vault.** The migration handles DB rows; the reconciler projects new stub frontmatter on its next tick. The vault catches up naturally.
- **Re-styling the review-queue UI.** Label + endpoint branch only. No new components, no CSS additions.

## Definition of done

- [ ] New review-queue UI shows external gaps with an "Approve (y)" button.
- [ ] Clicking it transitions the gap to `state=classified`; the next daily research tick picks it up.
- [ ] The data migration retroactively gates the existing ~210 `external+classified` rows.
- [ ] The new classifier (`opus-4-7@v2`) never produces fresh `external+classified` rows.
- [ ] Internal/hybrid pending behaviour is unchanged (still routes through `/verify`).
- [ ] All new tests + existing tests green on the branch's CI run.
- [ ] One end-of-PR code review completed against the full diff.
- [ ] PR merged via `--rebase`; live rollout verified per Task 6 Step 5.
