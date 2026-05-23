# Private Review Page Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a `/private/review` page on `private.jomcgi.dev` for manually reviewing knowledge-graph **gaps** (research/exploration areas) and **notes** (validate usefulness, mark public/private), with keyboard navigation and the ability to spot-check decisions previously made by automation.

**Architecture:**
A new SvelteKit route in the existing monolith frontend talks to new HTTP endpoints in `projects/monolith/knowledge/router.py`. Two new boolean columns — `gap.human_verified` and `note.visibility_verified` — distinguish "pending" items (no human has touched it) from "audit" items (automation decided; human spot-check needed). Both modes share a single keyboard-driven card UI; the URL holds tab + mode state. Subdomain-based rerouting (already in `frontend/src/hooks.js`) keeps the page off the public host; the Nav link only renders when the current host is `private.jomcgi.dev`.

**Tech Stack:**

- Backend: FastAPI, SQLAlchemy 2.x, Alembic, psycopg3 (prod), SQLite (tests via `create_all`)
- Frontend: SvelteKit with Node adapter, form actions + `use:enhance`
- CI: BuildBuddy (`bazel test //projects/monolith/...`)

---

## Key Constraints (read before any task)

1. **No local test loop.** Implement, commit, push. Watch CI via `gh pr checks <num> --watch`. Diagnose failures with `mcp__buildbuddy__*` tools.
2. **Worktree is `/tmp/claude-worktrees/private-review-page/`.** All commits land on `feat/private-review-page`.
3. **Conventional Commits.** `feat`, `fix`, `test`, etc. A `commit-msg` hook enforces this.
4. **psycopg3 nullable-param casts.** In any new `text(...)` SQL with nullable string params used in `IS NULL OR ...`, add explicit `::text` casts on every reference.
5. **SQLite test fixtures use `create_all`, not migrations.** Any CHECK constraint or default must be expressed in the SQLAlchemy `Column` definition so it carries over to SQLite. Don't rely on the migration to set the default — set it on the model too.
6. **Subagent boundaries.** Each task is its own commit on the same branch. Run `format` before each commit. Self-review the diff before committing. Defer all `bazel test` to the end-of-plan CI run.

---

## Pre-Task Context (subagents: read these first)

Before claiming any backend task, read:

- `projects/monolith/knowledge/router.py` (focus: lines around 560–584 for existing gap routes, and the `edit_note` route for the auth/serializer pattern)
- `projects/monolith/knowledge/models.py` (Gap model with state enum; Note model with visibility column)
- `projects/monolith/knowledge/gaps.py` (existing `answer_gap`, `get_review_queue` — mirror these patterns)
- `projects/monolith/knowledge/tests/conftest.py` (SQLite fixture setup)

Before claiming any frontend task, read:

- `projects/monolith/frontend/src/hooks.js` (subdomain rerouting)
- `projects/monolith/frontend/src/routes/+layout.svelte` (Nav + activeRoute)
- `projects/monolith/frontend/src/routes/+layout.server.ts` (or `.js` — server-side data)
- `projects/monolith/frontend/src/lib/public/components/Nav.svelte`
- `projects/monolith/frontend/src/routes/private/+page.svelte` (style/idiom reference)

---

### Task 1: Schema migration — `human_verified` / `visibility_verified` columns

**Files:**

- Modify: `projects/monolith/knowledge/models.py`
- Create: `projects/monolith/migrations/versions/<auto>_review_verification_columns.py` (filename will be Alembic-generated)
- Modify: `projects/monolith/knowledge/tests/conftest.py` if any fixture explicitly lists columns (most don't, but check)
- Test: `projects/monolith/knowledge/tests/test_models_verification.py`

**Step 1: Add columns to SQLAlchemy models**

In `projects/monolith/knowledge/models.py`, on the `Gap` class add:

```python
human_verified: Mapped[bool] = mapped_column(
    Boolean, nullable=False, server_default=text("false"), default=False
)
```

On the `Note` class add:

```python
visibility_verified: Mapped[bool] = mapped_column(
    Boolean, nullable=False, server_default=text("false"), default=False
)
```

Setting both `server_default` and `default` ensures:

- Postgres migration applies a backfill (`server_default`)
- SQLite tests via `create_all` get the same default (`default` is the SQLAlchemy-level default applied on insert)

**Step 2: Generate the Alembic migration**

From inside the worktree:

```bash
cd /tmp/claude-worktrees/private-review-page
direnv exec . alembic -c projects/monolith/alembic.ini revision --autogenerate -m "review verification columns"
```

Open the generated file. It should contain two `op.add_column(...)` calls. **Verify** that both columns have `server_default=sa.text('false')` and `nullable=False`. If autogen produced something different, edit it.

**Step 3: Write the failing test**

`projects/monolith/knowledge/tests/test_models_verification.py`:

```python
"""Verify new verification columns default to false on insert."""
from projects.monolith.knowledge.models import Gap, Note


def test_gap_human_verified_defaults_false(db_session):
    gap = Gap(term="test-term", context="ctx", gap_class="hybrid", state="discovered")
    db_session.add(gap)
    db_session.commit()
    db_session.refresh(gap)
    assert gap.human_verified is False


def test_note_visibility_verified_defaults_false(db_session):
    note = Note(id="test-note", title="Test")
    db_session.add(note)
    db_session.commit()
    db_session.refresh(note)
    assert note.visibility_verified is False
```

(Adapt construction args to match the real model signatures — read `models.py` first.)

**Step 4: Run tests via CI**

CI runs on push. No local pytest. Push at the end of all backend tasks (Task 3) and watch BuildBuddy.

**Step 5: Add BUILD entry if needed**

If the test file is new, register it in `projects/monolith/BUILD` (gazelle is excluded for the `knowledge/` subtree per memory — must be hand-added):

```python
py_test(
    name = "test_models_verification",
    srcs = ["knowledge/tests/test_models_verification.py"],
    deps = [
        "//projects/monolith/knowledge",
        "//projects/monolith/knowledge/tests:conftest",
        "@pip//pytest",
    ],
)
```

Confirm by reading existing `py_test` entries for sibling test files.

**Step 6: Commit**

```bash
format
git -C /tmp/claude-worktrees/private-review-page add -p
git -C /tmp/claude-worktrees/private-review-page commit -m "$(cat <<'EOF'
feat(monolith): add verification columns to gap and note

Tracks whether a human has verified an automation-made decision. Both
columns default false, so historical decisions appear in the audit queue
until reviewed.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Gap action endpoints — reject, verify, reopen, list-with-mode

**Files:**

- Modify: `projects/monolith/knowledge/router.py`
- Modify: `projects/monolith/knowledge/gaps.py`
- Test: `projects/monolith/knowledge/tests/test_gap_review_endpoints.py`

**Step 1: Add gap business-logic functions in `gaps.py`**

Add four functions, mirroring the structure of the existing `answer_gap`:

```python
def reject_gap(gap_id: int, db: Session) -> Gap:
    """Reject a pending gap. State: in_review -> rejected. Sets human_verified."""
    gap = _get_gap_or_404(gap_id, db)
    if gap.state != "in_review":
        raise HTTPException(409, f"gap {gap_id} not in review (state={gap.state})")
    gap.state = "rejected"
    gap.resolved_at = datetime.utcnow()
    gap.human_verified = True
    _tombstone_stub_if_present(gap, db)  # mirror answer_gap's cleanup
    db.commit()
    return gap


def verify_gap(gap_id: int, db: Session) -> Gap:
    """Acknowledge an auto-decided gap as correct. Sets human_verified=True."""
    gap = _get_gap_or_404(gap_id, db)
    if gap.state not in ("committed", "rejected", "parked"):
        raise HTTPException(409, f"gap {gap_id} not in a terminal state")
    gap.human_verified = True
    db.commit()
    return gap


def reopen_gap(gap_id: int, db: Session) -> Gap:
    """Reopen an auto-decided gap for fresh pending review. Clears human_verified."""
    gap = _get_gap_or_404(gap_id, db)
    if gap.state not in ("committed", "rejected", "parked"):
        raise HTTPException(409, f"gap {gap_id} not in a terminal state")
    gap.state = "in_review"
    gap.resolved_at = None
    gap.human_verified = False
    db.commit()
    return gap


def list_gaps_for_review(mode: str, limit: int, db: Session) -> list[Gap]:
    """
    mode='pending': state='in_review' AND gap_class IN ('internal','hybrid'),
        ordered by created_at ASC.
    mode='audit': state IN ('committed','rejected','parked') AND human_verified=False,
        ordered by resolved_at DESC.
    """
    if mode == "pending":
        return (
            db.query(Gap)
            .filter(Gap.state == "in_review")
            .filter(Gap.gap_class.in_(["internal", "hybrid"]))
            .order_by(Gap.created_at.asc(), Gap.id.asc())
            .limit(limit)
            .all()
        )
    if mode == "audit":
        return (
            db.query(Gap)
            .filter(Gap.state.in_(["committed", "rejected", "parked"]))
            .filter(Gap.human_verified.is_(False))
            .order_by(Gap.resolved_at.desc().nullslast(), Gap.id.desc())
            .limit(limit)
            .all()
        )
    raise HTTPException(400, f"unknown mode: {mode}")
```

Notes:

- `_get_gap_or_404` and `_tombstone_stub_if_present` may already exist as private helpers in `gaps.py`. If not, factor them out from `answer_gap`.
- The `answer_gap` function must **also** set `gap.human_verified = True` now — add one line. This keeps semantics consistent: any user-initiated decision marks verified.

**Step 2: Add HTTP routes in `router.py`**

In the same router file, alongside the existing gap routes:

```python
@router.post("/api/knowledge/gaps/{gap_id}/reject")
def post_reject_gap(gap_id: int, db: Session = Depends(get_db)):
    return _gap_response(reject_gap(gap_id, db))


@router.post("/api/knowledge/gaps/{gap_id}/verify")
def post_verify_gap(gap_id: int, db: Session = Depends(get_db)):
    return _gap_response(verify_gap(gap_id, db))


@router.post("/api/knowledge/gaps/{gap_id}/reopen")
def post_reopen_gap(gap_id: int, db: Session = Depends(get_db)):
    return _gap_response(reopen_gap(gap_id, db))
```

Replace `_gap_response` with whatever response shaping `answer_gap`'s route uses today.

The list endpoint at `/api/knowledge/gaps/review-queue` already exists for pending mode. **Either** extend it to accept `?mode=pending|audit` (default `pending` for back-compat), **or** add a new `/review-queue/audit` route. Pick the `?mode=` variant — fewer URLs to remember.

**Step 3: Write tests**

`projects/monolith/knowledge/tests/test_gap_review_endpoints.py`:

```python
def test_reject_transitions_to_rejected_and_verifies(client, db_session):
    gap = _make_gap(db_session, state="in_review", gap_class="hybrid")
    res = client.post(f"/api/knowledge/gaps/{gap.id}/reject")
    assert res.status_code == 200
    db_session.refresh(gap)
    assert gap.state == "rejected"
    assert gap.human_verified is True


def test_reject_rejects_non_pending(client, db_session):
    gap = _make_gap(db_session, state="committed")
    res = client.post(f"/api/knowledge/gaps/{gap.id}/reject")
    assert res.status_code == 409


def test_verify_sets_human_verified_without_state_change(client, db_session):
    gap = _make_gap(db_session, state="committed", human_verified=False)
    res = client.post(f"/api/knowledge/gaps/{gap.id}/verify")
    assert res.status_code == 200
    db_session.refresh(gap)
    assert gap.human_verified is True
    assert gap.state == "committed"


def test_reopen_moves_committed_back_to_in_review(client, db_session):
    gap = _make_gap(db_session, state="committed", human_verified=False)
    res = client.post(f"/api/knowledge/gaps/{gap.id}/reopen")
    assert res.status_code == 200
    db_session.refresh(gap)
    assert gap.state == "in_review"
    assert gap.human_verified is False


def test_list_audit_returns_only_unverified_terminal_gaps(client, db_session):
    keep = _make_gap(db_session, state="committed", human_verified=False)
    skip_verified = _make_gap(db_session, state="committed", human_verified=True)
    skip_pending = _make_gap(db_session, state="in_review")
    res = client.get("/api/knowledge/gaps/review-queue?mode=audit")
    ids = [item["id"] for item in res.json()]
    assert keep.id in ids
    assert skip_verified.id not in ids
    assert skip_pending.id not in ids
```

Add `_make_gap` helper to the test file or `conftest.py`.

**Step 4: Register the test target in `projects/monolith/BUILD`** (manual per gazelle exclusion).

**Step 5: Commit**

```bash
format
git -C /tmp/claude-worktrees/private-review-page commit -m "$(cat <<'EOF'
feat(monolith): add gap reject/verify/reopen endpoints and audit mode

Reject mirrors answer for the rejection terminal state. Verify and reopen
power the audit workflow: verify acknowledges an auto-decision as correct;
reopen sends an auto-decided gap back to in_review for fresh pending review.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Note action endpoints — list, set-visibility, verify-visibility, reset-visibility

**Files:**

- Modify: `projects/monolith/knowledge/router.py`
- Modify: `projects/monolith/knowledge/notes.py` (or wherever note write helpers live — verify before claiming)
- Test: `projects/monolith/knowledge/tests/test_note_review_endpoints.py`

**Step 1: Inventory before writing code**

`grep -rn 'def edit_note\b\|def update_note\b\|_PROMOTED_KEYS\|frontmatter' projects/monolith/knowledge/` to find the existing write path. The new endpoints should use the same low-level frontmatter-write helper to keep file-on-disk semantics consistent. **Do not** add new keys to `_PROMOTED_KEYS` — `visibility` should already be there. If it isn't, stop and ask before adding.

**Step 2: Add business-logic functions**

In whichever module owns note writes:

```python
def set_note_visibility(note_id: str, visibility: str, db: Session) -> Note:
    """Set visibility to 'public' or 'private'. Marks visibility_verified=True."""
    if visibility not in ("public", "private"):
        raise HTTPException(400, "visibility must be 'public' or 'private'")
    note = _get_note_or_404(note_id, db)
    _write_note_frontmatter(note, visibility=visibility)
    note.visibility = visibility
    note.visibility_verified = True
    db.commit()
    return note


def verify_note_visibility(note_id: str, db: Session) -> Note:
    """Acknowledge an auto-set visibility as correct. Flips visibility_verified only."""
    note = _get_note_or_404(note_id, db)
    if note.visibility is None:
        raise HTTPException(409, "cannot verify a note with no visibility set")
    note.visibility_verified = True
    db.commit()
    return note


def reset_note_visibility(note_id: str, db: Session) -> Note:
    """Reopen an auto-decided note for fresh pending review. Clears visibility + verified."""
    note = _get_note_or_404(note_id, db)
    _write_note_frontmatter(note, visibility=None)
    note.visibility = None
    note.visibility_verified = False
    db.commit()
    return note


def list_notes_for_review(mode: str, limit: int, db: Session) -> list[Note]:
    """
    mode='pending': visibility IS NULL, ordered by created_at ASC.
    mode='audit': visibility IS NOT NULL AND visibility_verified=False,
        ordered by updated_at DESC.
    """
    if mode == "pending":
        return (
            db.query(Note)
            .filter(Note.visibility.is_(None))
            .order_by(Note.created_at.asc(), Note.id.asc())
            .limit(limit)
            .all()
        )
    if mode == "audit":
        return (
            db.query(Note)
            .filter(Note.visibility.isnot(None))
            .filter(Note.visibility_verified.is_(False))
            .order_by(Note.updated_at.desc().nullslast(), Note.id.desc())
            .limit(limit)
            .all()
        )
    raise HTTPException(400, f"unknown mode: {mode}")
```

If `_write_note_frontmatter` doesn't exist as a clean helper, factor it out from `edit_note` — don't duplicate the file-write logic across functions. (DRY override of YAGNI here because two endpoints will hit it.)

**Step 3: Add HTTP routes**

```python
@router.get("/api/knowledge/notes/review-queue")
def get_notes_review_queue(
    mode: str = "pending", limit: int = 50, db: Session = Depends(get_db)
):
    notes = list_notes_for_review(mode, limit, db)
    return [_note_review_response(n) for n in notes]


@router.post("/api/knowledge/notes/{note_id}/visibility")
def post_set_visibility(
    note_id: str, body: dict, db: Session = Depends(get_db)
):
    return _note_review_response(
        set_note_visibility(note_id, body.get("visibility", ""), db)
    )


@router.post("/api/knowledge/notes/{note_id}/verify-visibility")
def post_verify_visibility(note_id: str, db: Session = Depends(get_db)):
    return _note_review_response(verify_note_visibility(note_id, db))


@router.post("/api/knowledge/notes/{note_id}/reset-visibility")
def post_reset_visibility(note_id: str, db: Session = Depends(get_db)):
    return _note_review_response(reset_note_visibility(note_id, db))
```

`_note_review_response` should return: `{id, title, snippet (first ~200 chars of body), tier, visibility, visibility_verified, updated_at}`.

**Step 4: Write tests** (parallel structure to Task 2's tests).

Cover: set to public, set to private, set with bad value → 400, verify, reset, list pending excludes those with visibility, list audit excludes verified.

**Step 5: Register test in `projects/monolith/BUILD`. Commit.**

```bash
format
git -C /tmp/claude-worktrees/private-review-page commit -m "$(cat <<'EOF'
feat(monolith): add note visibility review endpoints

Adds review-queue list (pending/audit modes) and three mutations:
set-visibility (pending decision), verify-visibility (audit agreement),
reset-visibility (audit disagreement, sends back to pending).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: SvelteKit route skeleton — `/private/review`

**Files:**

- Create: `projects/monolith/frontend/src/routes/private/review/+page.svelte`
- Create: `projects/monolith/frontend/src/routes/private/review/+page.server.ts`

**Step 1: Server load function**

`+page.server.ts`:

```ts
import type { PageServerLoad, Actions } from "./$types";
import { fail } from "@sveltejs/kit";

const API = process.env.MONOLITH_API ?? "http://localhost:8000";

export const load: PageServerLoad = async ({ url, fetch }) => {
  const tab = (url.searchParams.get("tab") ?? "gaps") as "gaps" | "notes";
  const mode = (url.searchParams.get("mode") ?? "pending") as
    | "pending"
    | "audit";

  const endpoint =
    tab === "gaps"
      ? `/api/knowledge/gaps/review-queue?mode=${mode}`
      : `/api/knowledge/notes/review-queue?mode=${mode}`;

  const res = await fetch(`${API}${endpoint}`);
  if (!res.ok)
    return { tab, mode, items: [], error: `Failed to load: ${res.status}` };
  return { tab, mode, items: await res.json() };
};

export const actions: Actions = {
  decide: async ({ request, fetch }) => {
    const data = await request.formData();
    const path = data.get("path") as string; // e.g. "/api/knowledge/gaps/42/reject"
    const res = await fetch(`${API}${path}`, { method: "POST" });
    if (!res.ok) return fail(res.status, { error: await res.text() });
    return { ok: true };
  },
  decideWithBody: async ({ request, fetch }) => {
    const data = await request.formData();
    const path = data.get("path") as string;
    const body = data.get("body") as string;
    const res = await fetch(`${API}${path}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body,
    });
    if (!res.ok) return fail(res.status, { error: await res.text() });
    return { ok: true };
  },
};
```

Two actions because note-visibility-set needs a JSON body (`{visibility: 'public'}`), while every other endpoint takes no body.

**Step 2: Page skeleton**

`+page.svelte`:

```svelte
<script lang="ts">
  import { page } from '$app/stores';
  import { goto, invalidateAll } from '$app/navigation';
  import ReviewCard from '$lib/private/components/ReviewCard.svelte';
  import ModeToggle from '$lib/private/components/ModeToggle.svelte';

  export let data: { tab: 'gaps'|'notes'; mode: 'pending'|'audit'; items: any[]; error?: string };

  let index = 0;
  $: current = data.items[index];
  $: tab = data.tab;
  $: mode = data.mode;

  function setTab(t: 'gaps'|'notes') {
    const url = new URL($page.url);
    url.searchParams.set('tab', t);
    goto(url, { replaceState: true, invalidateAll: true });
    index = 0;
  }

  function setMode(m: 'pending'|'audit') {
    const url = new URL($page.url);
    url.searchParams.set('mode', m);
    goto(url, { replaceState: true, invalidateAll: true });
    index = 0;
  }

  async function decide(action: string, item: any) {
    // action = 'yes' | 'no' | 'skip'
    // Map (tab, mode, action) -> endpoint path; defer to ReviewCard logic via prop.
    // Implementation in Task 6.
  }
</script>

<svelte:head><title>Review · private.jomcgi.dev</title></svelte:head>

<section>
  <header>
    <button class:active={tab==='gaps'} on:click={() => setTab('gaps')}>Gaps</button>
    <button class:active={tab==='notes'} on:click={() => setTab('notes')}>Notes</button>
    <ModeToggle {mode} on:change={(e) => setMode(e.detail)} />
  </header>

  {#if data.error}
    <p class="error">{data.error}</p>
  {:else if !current}
    <p>Queue empty.</p>
  {:else}
    <ReviewCard item={current} {tab} {mode} on:decide={(e) => decide(e.detail, current)} />
    <footer>{index + 1} / {data.items.length}</footer>
  {/if}
</section>
```

ReviewCard and ModeToggle are stubs at this point — they'll be filled in Tasks 5 and 6.

**Step 3: Commit**

```bash
format
git -C /tmp/claude-worktrees/private-review-page commit -m "$(cat <<'EOF'
feat(monolith-frontend): scaffold /private/review route

Adds the SvelteKit route, server load for both queues, and form actions
for decide endpoints. UI logic (keyboard, card, mode toggle) lands in
follow-up commits.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: ReviewCard component

**Files:**

- Create: `projects/monolith/frontend/src/lib/private/components/ReviewCard.svelte`
- Create: `projects/monolith/frontend/src/lib/private/components/ModeToggle.svelte`

**Step 1: ModeToggle**

```svelte
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  export let mode: 'pending' | 'audit';
  const dispatch = createEventDispatcher<{ change: 'pending' | 'audit' }>();
</script>

<div class="toggle" role="tablist">
  <button role="tab" aria-selected={mode==='pending'} on:click={() => dispatch('change', 'pending')}>Pending</button>
  <button role="tab" aria-selected={mode==='audit'} on:click={() => dispatch('change', 'audit')}>Audit auto-decisions</button>
</div>

<style>
  .toggle { display: inline-flex; gap: 0.25rem; }
  button[aria-selected="true"] { background: var(--accent); }
</style>
```

**Step 2: ReviewCard**

```svelte
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  export let item: any;
  export let tab: 'gaps' | 'notes';
  export let mode: 'pending' | 'audit';

  const dispatch = createEventDispatcher<{ decide: 'yes' | 'no' | 'skip' }>();
</script>

<article class="card">
  {#if tab === 'gaps'}
    <h2>{item.term}</h2>
    <dl>
      <dt>Context</dt><dd>{item.context}</dd>
      <dt>Class</dt><dd>{item.gap_class}</dd>
      {#if mode === 'audit'}
        <dt>Decided</dt><dd>{item.resolved_at} · state={item.state}</dd>
      {/if}
    </dl>
  {:else}
    <h2>{item.title}</h2>
    {#if item.snippet}<p class="snippet">{item.snippet}</p>{/if}
    <dl>
      <dt>Tier</dt><dd>{item.tier ?? '—'}</dd>
      {#if mode === 'audit'}
        <dt>Visibility</dt><dd>{item.visibility} · {item.updated_at}</dd>
      {/if}
    </dl>
  {/if}

  <div class="actions">
    {#if mode === 'pending'}
      <button on:click={() => dispatch('decide', 'yes')}>
        {tab === 'gaps' ? 'Keep (y)' : 'Public (y)'}
      </button>
      <button on:click={() => dispatch('decide', 'no')}>
        {tab === 'gaps' ? 'Reject (n)' : 'Private (n)'}
      </button>
    {:else}
      <button on:click={() => dispatch('decide', 'yes')}>Agree (y)</button>
      <button on:click={() => dispatch('decide', 'no')}>Re-open (n)</button>
      <button on:click={() => dispatch('decide', 'skip')}>Skip (s)</button>
    {/if}
  </div>
</article>
```

Style minimally — match existing private dashboard idioms. Don't overdesign.

**Step 3: Commit**

```bash
format
git -C /tmp/claude-worktrees/private-review-page commit -m "$(cat <<'EOF'
feat(monolith-frontend): add ReviewCard and ModeToggle components

Renders one item per card with mode-aware action buttons. Card dispatches
yes/no/skip events; the parent route maps these to HTTP endpoints.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Keyboard handler + decide() wiring

**Files:**

- Modify: `projects/monolith/frontend/src/routes/private/review/+page.svelte`

**Step 1: Map (tab, mode, action) → endpoint path**

Add to the script block:

```ts
function endpointFor(
  tab: "gaps" | "notes",
  mode: "pending" | "audit",
  action: "yes" | "no",
  item: any,
): { path: string; body?: string } {
  if (tab === "gaps") {
    if (mode === "pending") {
      return {
        path:
          action === "yes"
            ? `/api/knowledge/gaps/${item.id}/answer` // Note: pending-yes for gaps needs an answer string
            : `/api/knowledge/gaps/${item.id}/reject`,
      };
    }
    return {
      path:
        action === "yes"
          ? `/api/knowledge/gaps/${item.id}/verify`
          : `/api/knowledge/gaps/${item.id}/reopen`,
    };
  }
  // notes
  if (mode === "pending") {
    const visibility = action === "yes" ? "public" : "private";
    return {
      path: `/api/knowledge/notes/${item.id}/visibility`,
      body: JSON.stringify({ visibility }),
    };
  }
  return {
    path:
      action === "yes"
        ? `/api/knowledge/notes/${item.id}/verify-visibility`
        : `/api/knowledge/notes/${item.id}/reset-visibility`,
  };
}
```

**OPEN POINT** for the implementer to flag in the PR: `gaps/{id}/answer` requires an `answer` body, but the review-pending "yes" action just means "keep this gap — yes it's worth tracking." Two options:

- (a) The implementer adds a one-line `answer="(kept on review)"` placeholder when "yes" is hit in pending mode, so the existing `/answer` endpoint can be reused.
- (b) Add a separate `/api/knowledge/gaps/{id}/keep` endpoint that commits without requiring an answer string.

**Recommendation:** (b). The verb mismatch in (a) is confusing — "answer" implies content. Add `keep_gap` alongside `reject_gap` in Task 2 and a `/keep` route. Update Task 2's commit before pushing if not already done.

If Task 2 has already been committed without `/keep`, add it as a small fix-up commit before Task 6's wiring.

**Step 2: Optimistic update + form submit**

Replace the `decide` stub with:

```ts
async function decide(action: "yes" | "no" | "skip", item: any) {
  if (action === "skip") {
    advance();
    return;
  }

  const { path, body } = endpointFor(tab, mode, action, item);
  const form = new FormData();
  form.set("path", path);
  if (body) form.set("body", body);

  const actionUrl = body ? "?/decideWithBody" : "?/decide";

  // Optimistic: advance immediately; rollback on failure.
  const prevIndex = index;
  advance();

  const res = await fetch(actionUrl, { method: "POST", body: form });
  if (!res.ok) {
    index = prevIndex;
    // surface error inline
  } else if (data.items.length - index <= 3) {
    // Near the end — refetch the queue
    invalidateAll();
  }
}

function advance() {
  if (index < data.items.length - 1) index++;
}
function back() {
  if (index > 0) index--;
}
```

**Step 3: Keyboard handler**

```svelte
<svelte:window on:keydown={handleKey} />

<script lang="ts">
  // ...
  function handleKey(e: KeyboardEvent) {
    if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
    if (!current) return;
    switch (e.key) {
      case 'j': case 'ArrowDown': advance(); break;
      case 'k': case 'ArrowUp': back(); break;
      case 'y': decide('yes', current); break;
      case 'n': decide('no', current); break;
      case 's': if (mode === 'audit') decide('skip', current); break;
      case 'Tab':
        e.preventDefault();
        setTab(tab === 'gaps' ? 'notes' : 'gaps');
        break;
      case 'm':
        setMode(mode === 'pending' ? 'audit' : 'pending');
        break;
    }
  }
</script>
```

**Step 4: Commit**

```bash
format
git -C /tmp/claude-worktrees/private-review-page commit -m "$(cat <<'EOF'
feat(monolith-frontend): wire keyboard nav and decide actions

j/k navigate, y/n decide, s skip (audit), Tab swaps tab, m toggles mode.
Optimistic UI advances on submit and rolls back on failure.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Nav update — REVIEW link on private host only

**Files:**

- Modify: `projects/monolith/frontend/src/lib/public/components/Nav.svelte`
- Modify: `projects/monolith/frontend/src/routes/+layout.svelte`
- Modify: `projects/monolith/frontend/src/routes/+layout.server.ts` (or `.js`)

**Step 1: Pass `isPrivate` from server**

In the root layout server load, derive from the request host:

```ts
export const load: LayoutServerLoad = async ({ request }) => {
  const host = request.headers.get("host") ?? "";
  return {
    isPrivate: host === "private.jomcgi.dev" || host.startsWith("private."),
  };
};
```

(Adapt to existing layout shape — if it already returns data, merge.)

**Step 2: Conditionally render Nav item**

In `Nav.svelte`, accept an `isPrivate` prop and add the link only when true:

```svelte
<script lang="ts">
  export let activeRoute: string;
  export let isPrivate: boolean = false;
</script>

<nav>
  <!-- existing items unchanged -->
  {#if isPrivate}
    <a href="/review" class:active={activeRoute === 'review'}>REVIEW</a>
  {/if}
</nav>
```

**Step 3: Wire `isPrivate` through layout**

In `+layout.svelte`:

```svelte
<script lang="ts">
  export let data;
  $: activeRoute = derivePath($page.url.pathname);  // existing logic
</script>

<Nav {activeRoute} isPrivate={data.isPrivate} />
<slot />
```

Update the `derivePath` (or equivalent) to recognise `/review` and return `'review'`.

**Step 4: Commit**

```bash
format
git -C /tmp/claude-worktrees/private-review-page commit -m "$(cat <<'EOF'
feat(monolith-frontend): add REVIEW nav item on private host only

Server-side host check passes isPrivate down to Nav; the REVIEW link is
omitted on public.jomcgi.dev so the route remains invisible there.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Push, watch CI, fix, merge

**Step 1: Push the branch**

```bash
git -C /tmp/claude-worktrees/private-review-page push -u origin feat/private-review-page
```

**Step 2: Open the PR**

```bash
cd /tmp/claude-worktrees/private-review-page
gh pr create --title "feat: private review page for gaps and notes" --body "$(cat <<'EOF'
## Summary
- New `/private/review` route on private.jomcgi.dev with keyboard navigation
- Pending mode (drain-to-zero) and Audit mode (spot-check auto-decisions)
- Two new boolean columns track human verification of automation-made decisions

## Test plan
- [ ] CI: format + bazel test //projects/monolith/... green
- [ ] Visit private.jomcgi.dev/review — Gaps tab loads pending items
- [ ] Press Tab — Notes tab loads pending items
- [ ] Press m — switches to Audit mode, shows previously-auto-decided items
- [ ] Press y on an audit item — disappears from queue (human_verified=true)
- [ ] Press n on an audit gap — reappears in Pending tab
- [ ] Check public.jomcgi.dev — REVIEW nav item is absent

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

**Step 3: Watch CI**

```bash
gh pr checks <num> --watch
```

On failure: `mcp__buildbuddy__get_invocation` (selector: commit SHA) → `get_target` → `get_log`. Quote the actual error. Fix in a follow-up commit on the same branch. Push. Repeat.

**Step 4: End-of-plan code review**

Per CLAUDE.md: **one comprehensive review per merged PR**, not per task. Use the `code-review` skill or dispatch `feature-dev:code-reviewer`. Address findings, push fixes.

**Step 5: Merge**

```bash
gh pr merge <num> --auto --rebase
```

(Auto-merge allowed because this is a single feature PR with full CI green.)

**Step 6: Manual smoke test post-deploy**

Wait for ArgoCD to sync (~5-10s after merge). Visit `https://private.jomcgi.dev/review`. Walk through one item in each tab × each mode. Confirm keyboard shortcuts. Confirm public site doesn't show the nav item.

If anything is broken in production: open a fix PR immediately.

---

## Out of scope (explicit)

- MCP tool surface for the new endpoints (deferred per memory `reference_claude_routine_api_quirks.md`)
- Filters (gap_class, tier, date range) in audit mode — UI hooks left for follow-up
- Bulk operations
- Undo for individual decisions (the `n` re-open path is the de-facto undo for visibility/state changes)
- Server-side analytics on decisions per session
