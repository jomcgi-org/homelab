# Ember Semgrep Demo Page Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Public demo page at jomcgi.dev/ember/semgrep: a single editable code snippet (python or javascript) scanned by the production warm fc-invoke semgrep Pro workload in ~1s, with a savings counter versus the ~11s median of comparable hosted single-file scan services.

**Architecture:** A new `/api/ember/semgrep` surface inside the existing `ember_public` domain (same-tier registration, Turnstile session, rate bucket, bounded semaphore queue), calling the existing `semgrep_scan.client.scan_files()` path to fc-invoke `/invoke/semgrep`. Frontend is a new SvelteKit route under `routes/public/ember/semgrep/` reusing the ember mini-site language (ember.css, topbar breadcrumb, same-origin proxies with the Set-Cookie Path rewrite). A single-row `demo_sg_savings` table accrues scan count and time saved, mirroring `demo_pg_savings`.

**Tech Stack:** FastAPI (FastMonolith module), SvelteKit, fc-invoke semgrep workload (unchanged), Atlas migration, Bazel/apko.

**Key facts for implementers (verified during design):**

- Scan client: `projects/monolith/semgrep_scan/client.py` `scan_files(files)` POSTs `{"files": [{"path": ..., "content": ...}]}` to `{FC_INVOKE_URL}/invoke/semgrep` with `auth_headers()` (shared.k8s_auth). Response: `{"findings": [{path, line, col, rule_id, severity, message}], "errors": [...]}` or `{"error": ...}`. Read timeout 90s. Warm scan wall time is ~1s.
- The response has NO dataflow_trace field (guest normalizes). Render findings with line highlight only. Trace rendering is a future enhancement, do not attempt it.
- fc-invoke gates callers by TokenReview: `FC_INVOKE_ALLOWED_CALLERS` currently `system:serviceaccount:monolith:monolith`. The public backend runs as `system:serviceaccount:monolith-public:monolith-public` and MUST be added (Task 6) or public scans 403.
- Public tier checklist applies (docs/runbooks/public-tier-checklist.md): same-origin proxies only (no `/api` on the public origin), both `monolith` and `monolith-public` chart bumps, live curl is the only real verification.
- `ember_public` is already registered on both tiers (`app/modules_public.py`, `app/modules_private.py`) and in `MONOLITH_DOMAINS`; extending it means NO new domain registration work.
- Baseline constant: 11s is the vendor's own median for hosted single-file MCP scans (an unreleased product; do NOT name it anywhere in code, copy, commits, or this repo). Label it generically, e.g. "hosted single-file scan services" or "a comparable hosted MCP scan". Keep it a single named constant, `HOSTED_SCAN_MEDIAN_MS`.
- Writing style: no em-dashes anywhere (repo rule).
- New `*_test.py` files need py_test targets; gazelle handles it in CI but the file must contain a literal `import pytest` for target generation.

---

### Task 1: Backend core (`ember_public/semgrep_core.py`)

Session-gated, size-capped, queue-bounded scan orchestration. No router yet.

**Files:**
- Create: `projects/monolith/ember_public/semgrep_core.py`
- Test: `projects/monolith/ember_public/semgrep_core_test.py`

**Step 1: Write failing tests**

```python
"""Tests for the semgrep demo core: validation, queueing, savings math."""

import asyncio

import pytest

from ember_public import semgrep_core


def test_validate_rejects_oversize():
    err = semgrep_core.validate_snippet("python", "x = 1\n" * 300)
    assert err is not None and "lines" in err


def test_validate_rejects_long_chars():
    err = semgrep_core.validate_snippet("python", "x" * 20_000)
    assert err is not None and "characters" in err


def test_validate_rejects_bad_language():
    assert semgrep_core.validate_snippet("rust", "fn main() {}") is not None


def test_validate_accepts_small_python():
    assert semgrep_core.validate_snippet("python", "import os\n") is None


def test_snippet_path_by_language():
    assert semgrep_core.snippet_path("python") == "snippet.py"
    assert semgrep_core.snippet_path("javascript") == "snippet.js"


def test_scan_rate_bucket_blocks_rapid_repeat():
    tag = "tag-rate-test"
    assert semgrep_core.check_and_record_scan(tag) is True
    assert semgrep_core.check_and_record_scan(tag) is False


@pytest.mark.anyio
async def test_queue_rejects_when_full():
    # Fill every slot and the whole waiting queue, then expect rejection.
    sem = semgrep_core._make_queue(slots=1, max_waiters=1)
    async with sem.slot():  # holds the only slot
        # one waiter is allowed to queue...
        async def waiter():
            async with sem.slot():
                pass

        t = asyncio.create_task(waiter())
        await asyncio.sleep(0.01)
        # ...the next is bounced immediately
        with pytest.raises(semgrep_core.QueueFullError):
            async with sem.slot():
                pass
        t.cancel()


def test_savings_delta_uses_baseline():
    assert semgrep_core.saved_ms(scan_ms=1000) == semgrep_core.HOSTED_SCAN_MEDIAN_MS - 1000
    assert semgrep_core.saved_ms(scan_ms=999_999) == 0  # never negative
```

**Step 2: Verify tests fail** (module does not exist). Do not run tests locally; this repo defers all test execution to CI. Confirm by inspection that the module does not exist, then move on.

**Step 3: Implement `semgrep_core.py`**

```python
"""Core logic for the public semgrep demo: validation, admission, savings.

Mirrors the demo-postgres core in this package (rate bucket, semaphore)
but adds a small bounded waiting queue so the UI can show queueing
instead of an instant busy.
"""

import asyncio
import contextlib
import os
import time

HOSTED_SCAN_MEDIAN_MS = 11_000
MAX_LINES = 200
MAX_CHARS = 8_000
LANGUAGES = {"python": "snippet.py", "javascript": "snippet.js"}

_SCAN_MIN_INTERVAL_S = 3.0
_BUCKET_MAX_AGE_S = 3600.0
_scan_buckets: dict[str, float] = {}


class QueueFullError(Exception):
    """Raised when every slot and every waiting position is taken."""


def validate_snippet(language: str, content: str) -> str | None:
    if language not in LANGUAGES:
        return "language must be python or javascript"
    if len(content) > MAX_CHARS:
        return f"snippet is limited to {MAX_CHARS} characters"
    if content.count("\n") + 1 > MAX_LINES:
        return f"snippet is limited to {MAX_LINES} lines"
    if not content.strip():
        return "snippet is empty"
    return None


def snippet_path(language: str) -> str:
    return LANGUAGES[language]


def check_and_record_scan(session_tag: str) -> bool:
    """One scan per session per _SCAN_MIN_INTERVAL_S. Prunes stale tags."""
    now = time.monotonic()
    for tag, ts in list(_scan_buckets.items()):
        if now - ts > _BUCKET_MAX_AGE_S:
            del _scan_buckets[tag]
    last = _scan_buckets.get(session_tag)
    if last is not None and now - last < _SCAN_MIN_INTERVAL_S:
        return False
    _scan_buckets[session_tag] = now
    return True


class _BoundedQueue:
    """Semaphore with a hard cap on waiters, so admission is bounded."""

    def __init__(self, slots: int, max_waiters: int) -> None:
        self._sem = asyncio.Semaphore(slots)
        self._max_waiters = max_waiters
        self._waiters = 0

    @property
    def waiting(self) -> int:
        return self._waiters

    @contextlib.asynccontextmanager
    async def slot(self):
        if self._sem.locked() and self._waiters >= self._max_waiters:
            raise QueueFullError
        self._waiters += 1
        try:
            await self._sem.acquire()
        finally:
            self._waiters -= 1
        try:
            yield
        finally:
            self._sem.release()


def _make_queue(slots: int, max_waiters: int) -> _BoundedQueue:
    return _BoundedQueue(slots=slots, max_waiters=max_waiters)


QUEUE = _make_queue(
    slots=int(os.environ.get("EMBER_SEMGREP_MAX_CONCURRENT", "3")),
    max_waiters=int(os.environ.get("EMBER_SEMGREP_MAX_WAITERS", "8")),
)


def saved_ms(scan_ms: int) -> int:
    return max(0, HOSTED_SCAN_MEDIAN_MS - scan_ms)
```

Note on the queue test: `test_queue_rejects_when_full` holds the slot via `async with`, so `slot()` must raise BEFORE incrementing `_waiters` when full (as written above).

**Step 4: Self-review, run `bazel/tools/format/fast-format.sh`, commit**

```bash
git add projects/monolith/ember_public/semgrep_core.py projects/monolith/ember_public/semgrep_core_test.py
git commit -m "feat(ember): semgrep demo core validation, admission queue, savings math"
```

---

### Task 2: Savings persistence (`demo_sg_savings`)

**Files:**
- Modify: `projects/monolith/ember_public/models.py` (add `DemoSgSavings` SQLModel, single-row, mirror `DemoPgSavings`)
- Modify: `projects/monolith/ember_public/db.py` (reuse the existing writer/reader engine helpers for the new table; follow whatever `demo_pg_savings` does, do not invent a new engine)
- Create: migration in `projects/monolith/chart/migrations/` (next Atlas sequence number; copy the `demo_pg_savings` migration as the template, including its GRANT statements to the public writer/reader roles, renamed for `demo_sg_savings`)
- Test: extend `projects/monolith/ember_public/` tests following the existing savings-accrual test pattern

**Table shape:** `id` (single row, fixed pk), `scans` bigint, `actual_ms` bigint, `saved_ms` bigint. Accrual: on each successful scan, `scans += 1`, `actual_ms += scan_ms`, `saved_ms += saved_ms(scan_ms)`. Missing table degrades by omitting the field from responses (copy the `demo_pg_savings` degrade behavior).

**Steps:** failing test for accrue + read; implement model + accrue/read functions in `semgrep_core.py`; migration file; `import pytest` literal in any new test file. Check the Atlas migration directory's `atlas.sum` handling: follow the pattern of the most recent migration commit (the repo pins the Atlas version; regenerate the sum the way the neighboring migrations did, see git log for the last migration PR).

**Gate (public-tier checklist item 1):** confirm the GRANT statements cover the new table for the exact roles `demo_pg_savings` grants. Quote them in the commit message.

Commit: `feat(ember): demo_sg_savings accrual table and migration`

---

### Task 3: Router endpoints

**Files:**
- Modify: `projects/monolith/ember_public/router.py` (or a new `semgrep_router.py` wired into `module.py` register functions, whichever matches how `bazel_router.py` was added; follow that precedent)
- Test: router tests with a faked `semgrep_scan.client.scan_files`

**Endpoints (prefix `/api/ember/semgrep`):**

- `POST /session`: mirror the postgres session mint exactly (Turnstile siteverify when `TURNSTILE_SECRET_KEY` set, else stub), cookie `demo_sg_session`, `Path=/api/ember/semgrep`, httpOnly, secure, SameSite=lax, max_age 3600.
- `POST /scan`: body `{language, content}`. Order of checks: session cookie present (401), `validate_snippet` (422 with the message), rate bucket (429 with `retry_after_s`), then `QUEUE.slot()` (QueueFullError -> 503 `{busy: true, waiting: N}`). Inside the slot: `t0 = time.monotonic()`, call `semgrep_scan.client.scan_files([{"path": snippet_path(language), "content": content}])` via `asyncio.to_thread` if the client is sync (check its signature; the MCP tool call site shows the usage). On success: accrue savings, return `{findings, errors, scan_ms, queued_ms, saved_ms, baseline_ms: HOSTED_SCAN_MEDIAN_MS}`. On client `{"error": ...}`: return 502 `{error}` without accruing. `queued_ms` = time between entering `slot()` and acquiring it.
- `GET /savings`: all-time row `{scans, actual_ms, saved_ms}`, 30s TTL cache (copy the postgres savings cache pattern).

**Steps:** failing router tests (session required, cap enforced, fake scan returns findings passthrough + savings accrue, busy path); implement; format; commit.

Commit: `feat(ember): public semgrep demo scan endpoints`

---

### Task 4: Frontend page and proxies

**Files:**
- Create: `projects/monolith/frontend/src/routes/public/ember/semgrep/+page.svelte`
- Create: `projects/monolith/frontend/src/routes/public/ember/semgrep/+page.server.js` (SSR seed: savings GET)
- Create: `projects/monolith/frontend/src/routes/public/ember/semgrep/api/session/+server.js` (proxy; rewrite `Path=/api/ember/semgrep` -> `Path=/ember/semgrep`, copy the postgres session proxy including its vitest)
- Create: `projects/monolith/frontend/src/routes/public/ember/semgrep/api/scan/+server.js` (proxy, 30s timeout)
- Create: `projects/monolith/frontend/src/routes/public/ember/semgrep/api/savings/+server.js` (proxy)
- Test: vitest beside the session proxy for the Path rewrite (copy `postgres/api/session/server.test.js`)

**Page structure (reuse the ember mini-site language, `.ember-site` + `ember.css` tokens, topbar breadcrumb `jomcgi.dev / ember / semgrep`):**

- Left rail: title + two-sentence lede ("the production security scanner behind this cluster's CI, pointed at your snippet"; Pro engine, ~1,600 rules, cross-function taint within the file).
- Editor panel: `<textarea>` styled mono with a line-number gutter (CSS counter or a simple pre gutter; no editor dependency, no new npm deps), language toggle (python | javascript), example picker that swaps the buffer, char/line counters that go warn-colored near the cap.
- Scan button + Turnstile widget (copy the postgres page wiring), disabled until session minted.
- Results panel: per-finding rows (severity badge, `rule_id`, message, `line:col`), clicking a finding highlights that line in the gutter. Show `scan_ms` prominently with a proportional bar against `baseline_ms` ("0.9s, hosted single-file scan services median ~11s"). Empty state: "no findings, edit the snippet or load an example".
- Queue state: while POST is in flight past ~1.5s show "queued" narration; on 503 busy show "demo is busy, N waiting, try again in a moment".
- Savings footer: "N snippets scanned, X min of scan time saved" from `/savings`.
- Reduced-motion: no new animation loops needed; keep any transitions CSS-only.

**Canned examples (ship exactly these, they are verified to fire Pro taint rules on the warm path):**

python, "command injection across functions":

```python
import os

from flask import Flask, request

app = Flask(__name__)


def build_command():
    tool = request.args.get("tool")
    return f"/usr/bin/{tool} --report"


@app.route("/run")
def run():
    os.system(build_command())
    return "started"
```

python, "SQL injection":

```python
import sqlite3

from flask import Flask, request

app = Flask(__name__)


@app.route("/user")
def user():
    name = request.args.get("name")
    db = sqlite3.connect("app.db")
    row = db.execute(f"SELECT * FROM users WHERE name = '{name}'").fetchone()
    return str(row)
```

javascript, "command injection across functions":

```javascript
const express = require("express");
const { exec } = require("child_process");

const app = express();

function buildCommand(req) {
  return "convert " + req.query.file + " out.png";
}

app.get("/convert", (req, res) => {
  exec(buildCommand(req), () => res.send("ok"));
});
```

javascript, "code injection via eval":

```javascript
const express = require("express");

const app = express();

app.get("/calc", (req, res) => {
  const result = eval(req.query.expr);
  res.send(String(result));
});
```

**Gotchas (all previously hit on the postgres page):**
- Bare hex in `.svelte` `<style>` blocks trips semgrep rule `svelte-hardcoded-color-in-style`; put any new hex in a colocated `.css` file or use `--em-*` tokens.
- `/ember` is already in the root layout `hideNav` regex, no layout change needed.
- Do not name any Svelte prop `state` (shadows the `$state` rune, SSR 500).
- Local `vite dev` SSR is broken repo-wide; verify via CI visual regression and live screenshots, not `vite dev`.

**Steps:** proxies + vitest first, then page; format; commit.

Commit: `feat(ember): /ember/semgrep page, editor, proxies`

---

### Task 5: Landing page door + copy

**Files:**
- Modify: `projects/monolith/frontend/src/routes/public/ember/+page.svelte` (add a 4th `.door` card linking `/ember/semgrep`: label "workload demo", title "Semgrep", one-line description "the CI security scanner, warm in a microVM, scanning your snippet in about a second"; check the `.doors` grid handles 4 cards at all breakpoints, adjust the grid template if it assumed 3)

Copy rules: follow ~/repos/cv CLAUDE.md voice constraints already applied to this page (no "X, not Y" antithesis, no applause lines, no em-dashes).

Commit: `feat(ember): landing door for the semgrep demo`

---

### Task 6: Env wiring + allowed callers

**Files:**
- Modify: `projects/monolith/deploy/values.yaml` AND the public deployment's env plumbing so the public backend gets `FC_INVOKE_URL=http://fc-invoke.monolith.svc.cluster.local:8080` (find where the public web deployment env is templated; the postgres demo added `EMBERVM_URL` the same way, copy that wiring including any monolith-public values file)
- Modify: fc-invoke chart values (`projects/firecracker/substrate/chart/values.yaml` or wherever `allowedCallers` is listed) to add `system:serviceaccount:monolith-public:monolith-public` to `FC_INVOKE_ALLOWED_CALLERS`

**Verify by render, not by eye:**

```bash
helm template monolith-public projects/monolith-public/chart/ -f projects/monolith-public/deploy/values.yaml | grep -A1 FC_INVOKE_URL
helm template fc-invoke projects/firecracker/substrate/chart/ -f projects/firecracker/substrate/deploy/values.yaml | grep ALLOWED_CALLERS
```

(Adjust chart paths to reality; the point is to see both values in rendered output.)

Commit: `feat(ember,fc-invoke): public tier semgrep scan wiring and caller allowlist`

---

### Task 7: Chart bumps

```bash
bazel/tools/git/bump-chart.sh projects/monolith          # couples monolith-public automatically
bazel/tools/git/bump-chart.sh projects/firecracker/substrate
```

Confirm each bump updated BOTH `Chart.yaml` and `deploy/application.yaml` targetRevision. Commit: `chore(ember,fc-invoke): chart bumps for semgrep demo`

---

### Task 8: PR, CI, review, merge, live verification

1. Push branch, open PR (rebase-merge repo; conventional title `feat(ember): public semgrep scan demo`).
2. One comprehensive end-of-PR code review against the full diff (repo cadence: per-PR, not per-task).
3. `gh pr checks <n> --watch`; iterate on failures via `mcp__buildbuddy__*` (quote errors verbatim before hypothesizing).
4. Merge on green (`gh pr merge --rebase`, update-branch first if BEHIND).
5. Post-deploy gates (checklist): poll rollout, then live verification, the only verification that counts:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://jomcgi.dev/ember/semgrep   # expect 200
```

Then a real browser-path check: mint session, scan the default example, confirm findings render and `demo_sg_savings` accrues (read via `/ember/semgrep/api/savings`). Screenshot for the record.
