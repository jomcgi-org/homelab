# Discord Multiplayer Agent UX Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task (repo default; one comprehensive code review per merged PR, tests run on CI only).

**Goal:** Implement ADR 035: conversational ack + live task checklist, stage-boundary steering for thread-scoped shared sessions, two-stage classifier (attention + depth), and living channel directives in Postgres.

**Architecture:** All changes live in the monolith chat module (`projects/monolith/chat/`) plus the goosecracker guest recipes (`projects/firecracker/goosecracker/guest/recipes/`). Sessions are already thread-keyed (`GoosecrackerSession.discord_thread`) with a pending-message queue; this plan adds structured stage progress emitted by recipes and rendered as an edited checklist message, steering consumed at stage boundaries, an attention gate ahead of `on_message`, a `chat` branch in the recipe router, and a `chat.channel_directive` table seeded from a git-tracked base prompt.

**Tech Stack:** Python (SQLModel/FastAPI monolith), discord.py bot, Postgres via Atlas migrations (`projects/monolith/chart/migrations/`), goose recipes on fc-invoke, in-cluster Qwen for classification.

**Companion spec:** [2026-07-02-discord-multiplayer-agent-ux-spec.md](2026-07-02-discord-multiplayer-agent-ux-spec.md) defines the behaviour and acceptance criteria. [ADR 035](../decisions/agents/035-discord-multiplayer-agent-ux.md) holds the rationale.

**Repo rules that override generic practice:**

- No local test runs. Write tests first, implement, commit, push; CI (BuildBuddy) is the verifier. Batch pushes per phase.
- New `chat/*_test.py` files need a hand-added `py_test` in `projects/monolith/BUILD` (gazelle is excluded there); copy the shape of `chat_goosecracker_test` at `projects/monolith/BUILD:824`.
- SQLite test fixtures use `create_all`: mirror any SQL CHECK constraints in `__table_args__` so tests exercise them.
- New tables served to prod need an Atlas migration in `projects/monolith/chart/migrations/YYYYMMDDhhmmss_<desc>.sql`; keep them small.
- Deploying monolith changes requires a manual chart bump: `projects/monolith/chart/Chart.yaml` version AND `projects/monolith/deploy/application.yaml` `targetRevision` together.
- Recipe YAML changes are guest content: fc-invoke's chart must also be bumped or the new recipes never reach guests (chart-version bot does not reliably bump fc-invoke on guest changes; do it manually).

**Phasing = PR boundaries.** Each phase below is one PR (worktree off main, conventional commits, one code review at PR end, merge on green CI before the next phase starts).

---

## Phase 1: Structured stage progress + checklist message

Today `goosecracker_progress.Progress` is an in-memory stdout tail (`text`, `done`, `notice`) polled every 1.5s by `_stream_goosecracker_progress()` (bot.py:730), which edits a single message with raw output. This phase makes progress structured and renders a checklist.

### Task 1.1: Stage marker protocol in progress buffer

**Files:**

- Modify: `projects/monolith/chat/goosecracker_progress.py`
- Test: `projects/monolith/chat/goosecracker_progress_stages_test.py` (new; register `py_test` in `projects/monolith/BUILD`)

**Steps:**

1. Write failing tests: recipes emit marker lines on stdout in the form `::stage::<index>::<state>::<title>` (states: `pending|running|done|failed|skipped`). `append()` must parse marker lines out of the tail (they never render as raw text) and maintain `Progress.stages: list[Stage]` (dataclass: `index`, `title`, `state`). A full plan announcement (`::stages::<n>` followed by n pending markers) replaces the stage list (re-planning after steering). Non-marker chunks behave exactly as today.
2. Implement: extend the `Progress` dataclass with `stages` and a `stages_version` counter (bumped on any stage change, so the renderer can cheaply detect "needs edit").
3. Register the test target in `projects/monolith/BUILD` (copy `chat_goosecracker_progress_test` shape).
4. Commit: `feat(goosecracker): parse stage markers into structured progress`

### Task 1.2: Checklist renderer + edit coalescing in the stream loop

**Files:**

- Modify: `projects/monolith/chat/bot.py` (`_stream_goosecracker_progress`, bot.py:730-764)
- Test: `projects/monolith/chat/bot_checklist_render_test.py` (new; register in BUILD)

**Steps:**

1. Write failing tests for a pure `render_checklist(progress) -> str` helper: `⬜/🔄/✅/❌/⏭️` per state; completed stages collapse to `✅ N earlier stages` when the rendering exceeds 1900 chars; failed stage shows its one-line reason (carried in the marker title); falls back to today's tail rendering when no stages exist (artifact recipes unchanged).
2. Write failing tests for edit gating: the stream loop only edits when `stages_version` changed since the last edit or `done` flipped (no more per-poll edits of identical content), and never more than once per 2s.
3. Implement renderer + gating in the existing 1.5s poll loop. Keep the 900s timeout and `done` handling as-is.
4. Commit: `feat(goosecracker): render stage checklist with coalesced message edits`

### Task 1.3: Emit stage markers from the agent recipe router

**Files:**

- Modify: `projects/firecracker/goosecracker/guest/recipes/agent.yaml` (and the sub-recipes it delegates to: `implement.yaml`, `plan.yaml`, `research.yaml`, `query.yaml`)
- Modify: fc-invoke chart version (manual bump, see repo rules)

**Steps:**

1. In `agent.yaml`, after classification, echo the plan announcement (`::stages::<n>` + pending markers) and bracket each sub-recipe delegation with `running`/`done` markers; emit `failed` with a one-line reason on error paths.
2. Keep marker emission in the router, not scattered through sub-recipes, so the protocol has one producer per session.
3. Bump the fc-invoke chart (`Chart.yaml` + `deploy/application.yaml`) so guests pick up the recipes.
4. Commit: `feat(goosecracker): emit stage markers from agent recipe router`

### Task 1.4: Immutable ack, separate checklist message

**Files:**

- Modify: `projects/monolith/chat/bot.py` (`_handle_agent_command` bot.py:636, `_handle_goosecracker_command` bot.py:590)
- Test: extend `projects/monolith/chat/bot_agent_prompt_echo_test.py`

**Steps:**

1. Write failing tests: the ack (prompt echo + concierge framing) is posted as message A and never passed to the stream editor; the stream editor creates and edits its own message B. Today the stream edits the deferred interaction response; split them.
2. Implement: post ack, then post a placeholder checklist message ("Planning...") whose id feeds `_stream_goosecracker_progress`.
3. Commit: `feat(goosecracker): split immutable ack from edited checklist message`

**Phase 1 gate:** push branch, `gh pr create`, watch CI, one code review, `gh pr merge --auto --rebase`. Verify live: trigger `/agent` in the home server, confirm ack + separately-edited checklist.

---

## Phase 2: Stage-boundary steering + multiplayer thread access

`continue_session` (goosecracker.py:175) already queues messages while `running=True`, but the queue drains only after the turn completes, and non-owner thread replies are rejected with a roast (bot.py:815). This phase feeds queued messages into the _running_ session at stage boundaries and opens steering to permitted thread participants.

### Task 2.1: Steering fetch surface for guests

**Files:**

- Modify: `projects/monolith/chat/goosecracker.py` (expose pending steering), the guest-facing progress/artifact sink endpoints (per-tier MCP surface from ADR 034)
- Test: `projects/monolith/chat/goosecracker_steering_test.py` (new; register in BUILD)

**Steps:**

1. Write failing tests: a `fetch_steering(thread_id, after_id)` API returns queued messages (author id, author tier, text, message id) in order and marks them delivered; delivered steering is appended to the session transcript with attribution.
2. Implement on the same authenticated guest surface that serves progress/artifact sinks (ADR 034 tiers apply; the tool is read-only).
3. Commit: `feat(goosecracker): expose pending steering messages to running sessions`

### Task 2.2: Recipe hook: consume steering between stages

**Files:**

- Modify: `projects/firecracker/goosecracker/guest/recipes/agent.yaml`
- Modify: fc-invoke chart bump

**Steps:**

1. Between stages, the router calls the steering tool; if messages are pending it may reword/add/skip remaining stages and re-emits the plan announcement (Task 1.1 re-planning path renders it).
2. A steering message that is a cancel (`stop`/`cancel`) ends the run: emit remaining stages as `skipped`, final `done`.
3. Commit: `feat(goosecracker): consume steering at stage boundaries`

### Task 2.3: Tier-gated multiplayer steering in the bot

**Files:**

- Modify: `projects/monolith/chat/bot.py` (`_maybe_handle_goosecracker_reply`, bot.py:815)
- Test: extend `projects/monolith/chat/bot_on_message_test.py`

**Steps:**

1. Write failing tests: any thread participant's reply enqueues steering if `acl.is_granted(guild, user, "agent", scope)` for the session's repo scope; a participant lacking the tier gets a short refusal naming the missing grant (keep `build_roast` for flavor); owner-only check removed.
2. Implement; steering messages get the 👀 reaction via the existing outbox reaction path (remember the DiscordOutbox CHECK: a reaction row carries only the reaction).
3. Commit: `feat(goosecracker): tier-gated steering from any thread participant`

**Phase 2 gate:** PR, CI, review, merge. Live check: second account steers a running session; cancel works.

---

## Phase 3: Attention classifier + ambient grants

### Task 3.1: Ambient grant kind + decision log table

**Files:**

- Create: `projects/monolith/chart/migrations/<ts>_chat_attention_decisions.sql`
- Modify: `projects/monolith/chat/models.py`, `projects/monolith/chat/acl.py`
- Test: extend `projects/monolith/chat/acl_test.py`; `projects/monolith/chat/models_db_constraints_test.py`

**Steps:**

1. Ambient enablement reuses `DiscordFeatureGrant` with `feature="ambient"`, `subject_id=""` (channel-wide), `scope=<channel_id>`. Add `acl.ambient_channels(guild_id) -> set[str]`. Test first.
2. New model + migration `chat.attention_decision`: `id`, `channel_id`, `message_id`, `decision` (`engage|ignore`), `confidence`, `directive_version`, `created_at`. CHECKs mirrored in `__table_args__`. Ignores are sampled (env `ATTENTION_IGNORE_SAMPLE_RATE`, default 0.1); engages always logged.
3. Commit: `feat(chat): ambient grant kind and attention decision log`

### Task 3.2: Attention gate in on_message

**Files:**

- Modify: `projects/monolith/chat/bot.py` (`on_message`, bot.py:562)
- Create: `projects/monolith/chat/attention.py`
- Test: `projects/monolith/chat/attention_test.py` (new; register in BUILD)

**Steps:**

1. Write failing tests for `attention.evaluate(message, directive) -> AttentionResult(engage, confidence)`: mention/reply-to-bot short-circuits to engage (no model call); non-ambient channels short-circuit to ignore (no model call); ambient channels call the classify-only fast model (in-cluster Qwen, same client as `build_roast`) with the channel directive + a small message window, JSON `{engage, confidence}`; threshold env `ATTENTION_THRESHOLD` default 0.8; model errors fail closed (ignore) with a logged notice.
2. Wire into `on_message` ahead of existing handling: engage routes into the same conversational agent path as a mention. Log per Task 3.1.
3. Commit: `feat(chat): attention gate with ambient fast-model classifier`

### Task 3.3: Mention-triggered agent conversation

**Files:**

- Modify: `projects/monolith/chat/bot.py`
- Test: extend `projects/monolith/chat/bot_on_message_test.py`

**Steps:**

1. Write failing tests: an @mention (or ambient engage) in a granted channel starts the same flow as `/agent` without the slash command: thread off the trigger message, ack, session. ACL check identical to `_handle_agent_command`.
2. Implement by extracting the body of `_handle_agent_command` into a shared `start_agent_flow(channel, user, prompt, repo)` used by both entry points.
3. Commit: `feat(chat): mention and ambient triggers share the /agent flow`

**Phase 3 gate:** PR, CI, review, merge, chart bump, live check in one opted-in channel.

---

## Phase 4: Depth router "chat" branch

### Task 4.1: chat route in agent.yaml

**Files:**

- Modify: `projects/firecracker/goosecracker/guest/recipes/agent.yaml`
- Create: `projects/firecracker/goosecracker/guest/recipes/chat.yaml`
- Modify: fc-invoke chart bump

**Steps:**

1. Add `chat` to the router's classification set with guidance: questions answerable from context/knowledge with no repo mutation or artifact route to `chat`; `chat.yaml` produces a single conversational reply through the existing reply sink and emits no stage plan (ack-only turns show no checklist, per spec).
2. Misroute escape hatch in `chat.yaml`: if the answer requires real work, reply saying so and re-submit with the corrected route (one hop max).
3. Commit: `feat(goosecracker): chat branch in the recipe router`

**Phase 4 gate:** PR, CI, review, merge, fc-invoke bump. Live check: a question gets one reply and no checklist; a task gets ack + checklist.

---

## Phase 5: Living channel directives

### Task 5.1: Directive tables + git seed

**Files:**

- Create: `projects/monolith/chart/migrations/<ts>_chat_channel_directive.sql`
- Create: `projects/monolith/chat/directives.py` + `projects/monolith/chat/directive_seed.md` (the git-tracked seed)
- Modify: `projects/monolith/chat/models.py`
- Test: `projects/monolith/chat/directives_test.py` (new; register in BUILD)

**Steps:**

1. Migration + models for `chat.channel_directive` and `chat.user_style_pref` exactly as specified in the spec's data-model table (partial unique index on `(channel_id) WHERE active`; mirror constraints in `__table_args__`).
2. `directives.get_active(channel_id)` seeds version 1 from `directive_seed.md` on first read for a channel (records `seed_ref` = seed file content hash).
3. `directives.propose_update(channel_id, new_text, user_id, message_id)` / `apply(proposal)` / `reset(channel_id, user_id)`: apply writes a new version row, flips `active`, keeps history. A guard rejects directive text containing tool/ACL/ambient-enablement instructions (keyword screen + the update prompt forbids it); rejection reasons returned for the bot to relay.
4. Commit: `feat(chat): channel directive storage with git seed and provenance`

### Task 5.2: Propose-then-confirm update flow

**Files:**

- Modify: `projects/monolith/chat/bot.py`, `projects/monolith/chat/attention.py` (directive consumers)
- Test: `projects/monolith/chat/directive_flow_test.py` (new; register in BUILD)

**Steps:**

1. Write failing tests: a tier-permitted style request ("keep replies short here") makes the bot reply with the proposed new directive text; a 👍 reaction on that proposal from the requester or an admin within 10 minutes applies it (reaction handler + pending-proposal table or in-memory TTL map keyed by proposal message id); expiry discards. `directive reset` applies immediately for tier-permitted users.
2. Wire consumers: the attention classifier (Task 3.2) and the conversational reply prompt both read `directives.get_active`; the requester's `user_style_pref` layers into the reply prompt at reply time. Stamp `directive_version` into attention decision logs.
3. Commit: `feat(chat): propose-then-confirm directive updates with per-user style layering`

**Phase 5 gate:** PR, CI, review, merge, chart bump. Live check per spec acceptance: style request round-trip, refused escalation request, reset lineage.

---

## Rollout and verification order

1. Phases are strictly sequential; each merges before the next starts (recipes and bot code co-evolve across the guest boundary).
2. After each monolith-affecting merge: manual chart bump (Chart.yaml + application.yaml), ArgoCD syncs, verify pod rollout via kubectl before live checks.
3. After recipe-affecting merges (1, 2, 4): fc-invoke chart bump too.
4. End state check runs the spec's acceptance list top to bottom in the home server before calling the plan done.
