# Ambient Assistant Parity Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task (repo default; one comprehensive code review per merged PR, tests run on CI only).

**Goal:** Implement ADR 043: catch-up summarization and decisions extraction as chat-agent tools, time-based reminders drained through the scheduler and outbox, channel-data charts via injected context to the artifact guest, and an observed-but-confirmed directive evolution job.

**Architecture:** Everything except Phase 3's recipe touch lives in `projects/monolith/chat/`. Features A and B are chat-route only: new tools on the pydantic-ai agent in `chat/agent.py` (`create_agent`), backed by a bounded window fetch on `MessageStore` and a new `chat.reminder` table drained by a monolith scheduler job into `discord_outbox`. Feature C extends the agent-escalation path so a dataset extracted host-side rides the ADR 040 `/injected-context/` seam into the artifact guest. Feature D is a weekly scheduler job that stages proposals through the existing `chat/directives.py` propose-then-confirm flow.

**Tech Stack:** Python (SQLModel/FastAPI monolith), pydantic-ai chat agent on in-cluster Qwen, discord.py bot + `discord_outbox`, Postgres via Atlas migrations (`projects/monolith/chart/migrations/`), scheduler jobs (`projects/monolith/scheduler/api.py::register_job`), fc-invoke guest recipes (Phase 3 only).

**Companion spec:** [2026-07-04-ambient-assistant-parity-spec.md](2026-07-04-ambient-assistant-parity-spec.md) defines behaviour and acceptance. [ADR 043](../decisions/agents/043-ambient-assistant-parity.md) holds the rationale.

**Repo rules that override generic practice:**

- No local test runs. Write tests first, implement, commit, push; BuildBuddy CI is the verifier. Batch pushes per phase.
- New `chat/*_test.py` files need a hand-added `py_test` in `projects/monolith/BUILD` (gazelle is excluded there); copy the shape of an existing `chat_*_test` target.
- SQLite test fixtures use `SQLModel.metadata.create_all`, not migrations: mirror SQL CHECK constraints in `__table_args__`, and remember TIMESTAMPTZ columns round-trip naive in tests (assert `isinstance(value, datetime)`, coerce before comparing).
- New tables need an Atlas migration `projects/monolith/chart/migrations/YYYYMMDDhhmmss_<desc>.sql`; the timestamp must sort after the newest migration on origin/main at merge time, and `atlas.sum` must be regenerated with the exact pinned CI version (Atlas community v1.1.0, not a homebrew atlas).
- Scheduler handlers are `async def handler(session) -> datetime | None` awaited on the event loop: do network I/O with `await`, then delegate ALL Session work to `await asyncio.to_thread(_sync_helper, plain_data)` where the helper opens its own session. Semgrep enforces this (`no-sync-session-in-async-def`, `no-session-in-to-thread`, `session-add-in-loop`).
- Deploying monolith changes needs a chart bump in the same PR: `bazel/tools/git/bump-chart.sh projects/monolith`. Phase 3's recipe change also needs the fc-invoke chart bumped or guests never see it.
- Run `bazel/tools/format/fast-format.sh` before each commit.

**Phasing = PR boundaries.** Each phase is one PR (worktree off main, conventional commits, one comprehensive code review at PR end, merge on green CI before the next phase starts).

---

## Phase 1: Catch-up + decisions extraction tools (Feature A)

The chat agent (`chat/agent.py::create_agent`, tools registered from line ~270) gets two tools over a new bounded window fetch. `ChatDeps` (agent.py:96) already carries `channel_id`, `store` (a `MessageStore`), and `author_id`; Discord threads are channels, so `channel_id` scoping covers both.

### Task 1.1: Bounded window fetch on MessageStore

**Files:**

- Modify: `projects/monolith/chat/store.py`
- Test: `projects/monolith/chat/store_fetch_window_test.py` (new; register `py_test` in `projects/monolith/BUILD`)

**Steps:**

1. Write failing tests for `MessageStore.fetch_window(channel_id, *, max_messages=300, max_chars=30_000) -> list[Message]`: returns chronological (oldest first) messages for exactly that channel; stops at whichever cap hits first (drop oldest, keep newest); empty channel returns `[]`; other channels' rows never leak. Seed via the existing SQLite fixture pattern used by the other `store_*_test.py` files.
2. Implement: newest-first query, apply caps, reverse to chronological. Reuse the store's existing session/engine access pattern; do not add a second query path.
3. Register the test target in `projects/monolith/BUILD`.
4. Commit: `feat(chat): bounded chronological window fetch on MessageStore`

### Task 1.2: Chunked digest helper (pure, caller-injected)

**Files:**

- Create: `projects/monolith/chat/digest.py`
- Test: `projects/monolith/chat/digest_test.py` (new; register in BUILD)

**Steps:**

1. Write failing tests for `async digest_window(messages, mode, caller) -> str` with `mode in {"summary", "decisions"}` and `caller` an injectable async LLM callable (same shape as `summarizer.build_llm_caller()`), so tests use a fake caller and never touch a model:
   - formats messages as `[HH:MM] display_name: content` lines;
   - a window under ~8k chars makes exactly one caller invocation;
   - a larger window is split into chunks, each summarized, then reduced with one final call (assert call count and that the reduce prompt contains the chunk outputs);
   - the returned string is prefixed with a coverage line: `(window: N messages, back to <ISO of oldest>)`;
   - `decisions` mode's prompt asks for decisions / action items (with who) / open questions and instructs "leave unattributed rather than guess".
2. Implement. Prompts live as module constants. No Discord or DB imports in this module.
3. Register the test target.
4. Commit: `feat(chat): chunked window digest helper for summary and decisions modes`

### Task 1.3: `catch_up` and `extract_decisions` agent tools

**Files:**

- Modify: `projects/monolith/chat/agent.py` (`create_agent`, tool block from ~line 270; system prompt in `build_system_prompt`)
- Test: `projects/monolith/chat/agent_channel_digest_tool_test.py` (new; register in BUILD)

**Steps:**

1. Write failing tests (copy the harness style of `agent_tool_execution_test.py`): `catch_up(ctx)` fetches the window via `ctx.deps.store.fetch_window(ctx.deps.channel_id)` and returns `digest_window(..., mode="summary", ...)`; `extract_decisions(ctx)` same with `mode="decisions"`; both return a plain "nothing to summarize yet" string on an empty window instead of calling the model.
2. Implement both as `@agent.tool` functions, building the caller with `summarizer.build_llm_caller()` (lazy import inside the tool, matching how other tools reach chat internals). Add one system-prompt paragraph: use `catch_up` for "catch me up / summarize this thread / what happened here", `extract_decisions` for "what did we decide / action items / open questions", and always relay the tool's coverage line.
3. Register the test target.
4. Commit: `feat(chat): catch-up and decisions-extraction tools over the channel window`

### Task 1.4: Keep these requests on the chat route

**Files:**

- Modify: `projects/monolith/chat/attention.py` (`needs_agent` prompt, attention.py:94-127)
- Test: extend `projects/monolith/chat/attention_test.py`

**Steps:**

1. Write failing tests using the injectable `_caller`: assert the classify prompt text now contains an explicit instruction that summarizing or extracting decisions from THIS conversation/channel history is `"chat"`, and that repo/artifact/research wording is unchanged.
2. Implement the one-sentence prompt addition.
3. Commit: `fix(chat): depth classifier keeps channel summarization on the chat route`

**Phase 1 gate:** `bazel/tools/git/bump-chart.sh projects/monolith`, push branch, `gh pr create`, watch `gh pr checks --watch`, one comprehensive code review, `gh pr merge --auto --rebase`. Verify live per spec acceptance A1-A4 ("catch me up" in a busy thread; a repo question still escalates).

---

## Phase 2: Reminders (Feature B)

### Task 2.1: `chat.reminder` table + model

**Files:**

- Create: `projects/monolith/chart/migrations/<next-timestamp>_chat_reminder.sql`
- Modify: `projects/monolith/chat/models.py`
- Test: `projects/monolith/chat/models_reminder_test.py` (new; register in BUILD)

**Steps:**

1. Write failing model tests: a `Reminder` row round-trips; status defaults to `pending`; a CHECK limits status to `pending|delivered|cancelled` (mirror in `__table_args__` so SQLite enforces it too).
2. Add the SQLModel: `id` (pk), `channel_id: str`, `author_id: str`, `content: str`, `due_at: datetime` (TIMESTAMPTZ), `status: str`, `created_at`, `delivered_at: datetime | None`. Table name `reminder`, schema `chat` (match how existing chat models declare schema).
3. Write the migration: CREATE TABLE mirroring the model, plus an index on `(status, due_at)` for the drain query. Timestamp the filename after the newest migration on origin/main. Regenerate `atlas.sum` with the pinned Atlas community v1.1.0.
4. Register the test target. Commit: `feat(chat): reminder table and model`

### Task 2.2: Reminder CRUD + drain core (sync, session-parameterized)

**Files:**

- Create: `projects/monolith/chat/reminders.py`
- Test: `projects/monolith/chat/reminders_test.py` (new; register in BUILD)

**Steps:**

1. Write failing tests against sync functions that take an explicit `session` (SQLite `create_all` fixture drives them directly):
   - `create_reminder(session, channel_id, author_id, content, due_at) -> Reminder | str`: returns an error string (not an exception) when `due_at` is not in the future, is more than 366 days out, or the author already has 10 pending reminders;
   - `list_pending(session, author_id)` ordered by `due_at`;
   - `cancel_reminder(session, author_id, reminder_id) -> bool`: only the author's own pending rows cancel;
   - `deliver_due(session, now) -> int`: for each pending row with `due_at <= now`, enqueue `outbox.enqueue_message(session, channel_id, content=f"⏰ <@{author_id}> reminder: {content}")`, flip status to `delivered`, set `delivered_at`; returns count; caller commits (matches the chat write-API convention);
   - `next_due(session) -> datetime | None` over pending rows.
   - Naive-datetime coercion per the SQLite fixture rule when comparing.
2. Implement. Build rows and `add_all` (no `session.add` in a loop).
3. Register the test target. Commit: `feat(chat): reminder CRUD and due-drain core`

### Task 2.3: Scheduler job

**Files:**

- Create: `projects/monolith/chat/jobs.py` (or extend the module where chat-side jobs already register; grep `register_job(` call sites and follow that wiring, e.g. `hikes/jobs.py` registration)
- Test: `projects/monolith/chat/jobs_reminder_test.py` (new; register in BUILD)

**Steps:**

1. Write failing tests for the sync core `_drain_reminders() -> datetime | None`: opens its own session, calls `deliver_due(session, utcnow)`, commits, returns `next_due` (the scheduler's next-run hint). The async handler is a thin `await asyncio.to_thread(_drain_reminders)` wrapper and is not unit tested (repo convention).
2. Implement and register the job with `scheduler.api.register_job` following the hikes pattern (name `chat-reminders`, modest default interval so an idle queue costs nothing; the `next_due` return tightens the wakeup when something is pending).
3. Register the test target. Commit: `feat(chat): scheduler job drains due reminders into the outbox`

### Task 2.4: Agent tools + temporal confirmation

**Files:**

- Modify: `projects/monolith/chat/agent.py`
- Test: `projects/monolith/chat/agent_reminder_tools_test.py` (new; register in BUILD)

**Steps:**

1. Write failing tool tests: `set_reminder(ctx, due_at_iso, text)` parses ISO 8601 (reject non-ISO with a plain-language error string), calls `create_reminder` with `ctx.deps.channel_id`/`ctx.deps.author_id` via `asyncio.to_thread`, and returns a confirmation containing the absolute UTC time; `list_my_reminders(ctx)`; `cancel_reminder(ctx, reminder_id)`. Error strings from the CRUD layer pass through verbatim.
2. Implement the three `@agent.tool` functions plus a system-prompt paragraph: convert natural-language times using the temporal grounding already in the prompt, always confirm with the resolved absolute time, reminders are one-shot.
3. Register the test target. Commit: `feat(chat): reminder tools on the chat agent`

**Phase 2 gate:** chart bump, PR, CI, review, merge. Verify live per spec B1-B4 (2-minute reminder round-trip; list/cancel; cap refusal).

---

## Phase 3: Channel-data artifacts (Feature C)

The fiddly phase: it touches the escalation path and the guest boundary. Read ADR 040 and `chat/orchestrator.py` before starting; keep the fail-open invariant (extraction failure must never block a dispatch).

### Task 3.1: Dataset extraction helper

**Files:**

- Create: `projects/monolith/chat/channel_data.py`
- Test: `projects/monolith/chat/channel_data_test.py` (new; register in BUILD)

**Steps:**

1. Write failing tests for `async extract_dataset(messages, request, caller) -> str | None`: prompts the injected caller to emit strict JSON `{"title": str, "columns": [str], "rows": [[...]]}` from the window given the user's request; validates and re-serializes (invalid JSON, missing keys, or >200 rows returns `None`, never raises); includes a `source_window` field with message count and oldest timestamp.
2. Implement. Pure module, caller-injected, mirroring `digest.py`.
3. Register the test target. Commit: `feat(chat): structured dataset extraction from a channel window`

### Task 3.2: Inject the dataset into the artifact dispatch

**Files:**

- Modify: the agent-escalation path where injected context is assembled (`build_injected_context` producer, ADR 040; grep `build_injected_context` for the call site in the dispatch flow)
- Test: extend the existing injected-context tests alongside that producer

**Steps:**

1. Write failing tests: when an artifact-route dispatch carries a non-`None` extracted dataset, the injected-context bundle gains a `channel-data.json` entry with that payload; when extraction returned `None` or the route is not artifact, the bundle is byte-identical to today.
2. Implement: in the artifact escalation path, when the request references channel content (cheap heuristic: the orchestrator/brief already classifies; when in doubt, attempt extraction and let `None` mean "no file"), fetch the window (Task 1.1), call `extract_dataset`, attach on success. Wrap the whole step in the same fail-open guard style as the orchestrator (`compile` fail-open).
3. Commit: `feat(chat): ship extracted channel datasets to artifact guests via injected context`

### Task 3.3: Teach the artifact recipe about the file

**Files:**

- Modify: the artifact guest recipe under fc-invoke's guest recipes (grep the recipes dir for the artifact recipe prompt)
- Chart: bump fc-invoke's chart as well as monolith's

**Steps:**

1. Add one recipe-prompt paragraph: if `/injected-context/channel-data.json` exists, render the artifact from it and cite its `source_window`; if absent, proceed from the prompt alone.
2. Commit: `feat(goosecracker): artifact recipe renders injected channel datasets`

**Phase 3 gate:** bump BOTH monolith and fc-invoke charts, PR, CI, review, merge. Verify live per spec C1-C3 (chart request renders real numbers; a plain artifact request is unchanged).

---

## Phase 4: Directive evolution observer (Feature D, optional)

Drop this phase if Joe deprioritizes it; nothing earlier depends on it.

### Task 4.1: Friction classifier (pure, caller-injected)

**Files:**

- Create: `projects/monolith/chat/observer.py`
- Test: `projects/monolith/chat/observer_test.py` (new; register in BUILD)

**Steps:**

1. Write failing tests for `async find_style_friction(exchanges, caller) -> dict | None`: given formatted recent bot-involved exchanges (user messages replying to or mentioning the bot), the injected caller is asked for strict JSON `{"friction": bool, "directive_change": str, "evidence_message_ids": [str]}`; requires >= 3 evidence ids or returns `None`; malformed output returns `None`.
2. Implement. Commit: `feat(chat): style-friction classifier for directive proposals`

### Task 4.2: Weekly observer job, propose-then-confirm preserved

**Files:**

- Modify: `projects/monolith/chat/jobs.py`, `projects/monolith/chat/directives.py` (reuse `propose_update`), the outbox/bot seam that posts proposal messages (study how `pending_proposal` entries from the `propose_directive_update` tool get posted with confirm reactions in `_stream_response`, and reuse or extract that path so the job's proposals get the same reaction-confirm handling)
- Test: `projects/monolith/chat/jobs_observer_test.py` (new; register in BUILD)

**Steps:**

1. Write failing tests for the sync core: iterates ambient-granted channels only; skips a channel with an open proposal or one resolved within 14 days; on friction, stages via `propose_update` and produces exactly one proposal post (with evidence links) per run per channel.
2. Implement the weekly job. The proposal-message posting is the fiddly bit: the agent-tool path posts from bot context and records `proposal_message_id` for the reaction handler; the job path must end up with the same linkage. If the outbox cannot return the posted message id, route the job's proposal through the bot-side drain that CAN (document the choice in the module docstring).
3. Register the test target. Commit: `feat(chat): weekly observer proposes directive updates from repeated corrections`

**Phase 4 gate:** chart bump, PR, CI, review, merge. Verify live per spec D1-D3 against a test channel with seeded corrections.

---

## Rollout and verification order

1. Phase 1 ships first: read-only, chat-route, biggest perceived win.
2. Phase 2 second: first proactive write path, all on existing durable primitives.
3. Phase 3 third: guest-boundary work, needs the Phase 1 window fetch.
4. Phase 4 last and optional.

Each phase is independently valuable and independently revertible (revert = revert the PR and chart bump; Phase 2/4 tables are additive and inert once their jobs are deregistered).
