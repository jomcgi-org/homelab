# /improve-ambient Skill Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a `/improve-ambient` skill: an offline, evidence-backed feedback loop over ambient Discord chat activations that scores each engagement for fluidity + productivity and opens PRs tuning the ambient prompt/gate, and stages channel directives where behaviour needs to diverge per-channel.

**Architecture:** Sibling of `/improve-recipes` and `/improve-artifacts`. Three PRs. **PR 1** adds reaction persistence to the monolith (a new `chat.reaction_event` table + handler writes) so human 👍/👎/🤖 on Bosun's ambient replies become a queryable ground-truth signal, which does not exist today. **PR 2** adds the on-demand skill: an in-pod Python helper (piped via `kubectl exec`, read-only Postgres, S3 for eval JSON, exactly like the sibling `improve_recipes_tool.py`) plus a `SKILL.md` carrying a versioned taxonomy, a lever routing table, and the three-level routing rule that answers "where is a divergence protocol required" (code lever vs channel directive vs personal style pref). **PR 3** adds the **directive autopilot**: a scheduled routine (extends `observer_job`'s Argo CronWorkflow pattern) that, from the same episode signals, **silently** auto-applies high-confidence low-risk channel + personal directive updates in the background (no channel messages, ever), baselines, and self-validates against the next window's reactions, auto-reverting on regression. Instead of announcing in-channel, it **exposes the current directives and its own apply/revert provenance on an introspection surface** (MCP tools over the directive history + autopilot log) for out-of-band review and manual tuning. A human manual tune always wins over the autopilot (a `source` precedence rule). Code levers (prompts/gate) are NEVER touched autonomously; they only change through PR 2's reviewed path.

**Tech Stack:** Python (SQLModel, FastAPI monolith), discord.py, Atlas migrations, Argo CronWorkflow, S3 (artifacts bucket), Claude Code skill markdown. No new services. Deployment is a single monolith chart bump per PR (no guest apko rebuild; all levers live in the monolith backend).

**Model routing:** PR 1 tasks are well-specified locally-verifiable edits — dispatch to Sonnet implementers. PR 2's helper SQL / taxonomy design and PR 3's autopilot gate + self-validation logic carry more judgment — keep those on Opus. One comprehensive Opus code review per PR (not per task), per repo convention.

**Sequencing:** PR 1 → PR 2 → PR 3. PR 1 merges first (the reaction signal only accrues forward from its migration). PR 2 does **not** hard-block on reaction volume: its proxy signals (attention decisions, human follow-up, agent-thread results) work immediately, so the skill can run report-only on day one and gain reaction fidelity as data accumulates. PR 3 depends on PR 1 (needs reaction evidence to gate/validate) and should land only after PR 2's taxonomy has been sanity-checked on a real window, so the autopilot inherits a proven classifier rather than an unvetted one.

**Autonomy boundary (load-bearing):** the autopilot may only mutate **directive text** (channel directives + per-user style prefs), which are reversible and self-validating via downstream reactions. It must never edit code levers (`agent.py`/`summarizer.py`/`attention.py`) or touch ACLs/grants. `directives.guard()` still screens every proposed/applied directive.

---

## Background: what exists today (read before starting)

- **Ambient engage path:** `projects/monolith/chat/bot.py` ~767-808 checks `acl.ambient_channels(guild_id)`, then `attention.evaluate()` (`chat/attention.py`) classifies engage/ignore, logging to `chat.attention_decision`. Engaged messages split via `attention.needs_agent()` into a goose agent run (`_engage_agent`) or an in-monolith reply (`_process_message(force_respond=True)`).
- **The prompt levers** the skill will tune:
  - Ambient/base system prompt: `chat/agent.py` `build_system_prompt()` (~174-266) and the per-turn directive injection `_channel_directive` (~335-356).
  - Agent-result first-person delivery: `chat/summarizer.py` `_build_agent_reply_prompt()` (~366-398).
  - Attention gate prompt + thresholds: `chat/attention.py` `evaluate()` prompt (~58-82), `ATTENTION_THRESHOLD` (0.5), `_RECENT_TAG_THRESHOLD` (0.35).
- **Channel directives (the divergence protocol):** `chat/directives.py` `get_active`, `propose_update`, `apply_proposal`, `guard`. `guard()` keyword-blocks text containing `tool`, `grant`, `acl`, `permission`, `ambient`, `repo`, `admin` (case-insensitive). Directive rows live in `chat.channel_directive` (versioned history, one active row per channel). The runtime observer `chat/observer_job.py` already auto-proposes directives; this skill is its offline human-in-the-loop counterpart and must reuse `propose_update`, not a new path.
- **Personal directives (per-user style prefs):** `chat/directives.py` `get_style_pref(user_id)` / `set_style_pref(user_id, pref, motivating_message_id)`, rows in `chat.user_style_pref` (`models.py` ~386, versioned, one active per user, `active` flag). Already injected per-turn by `_channel_directive` ("This user's style preference: {pref}"). NOTE: `set_style_pref` sets **directly** — there is no propose-then-confirm flow for personal prefs. So personal changes are auto-apply-with-revert only (never a 👍 proposal); channel directives keep both paths.
- **Provenance / manual precedence (added by PR 3):** both `channel_directive` and `user_style_pref` gain a `source` column (`seed`|`observer`|`autopilot`|`manual`). The autopilot never overrides an active `manual` row within a cooldown, so out-of-band manual tuning wins. Existing rows backfill to `seed`/`observer`.
- **Sibling skill to mirror exactly:** `.claude/skills/improve-recipes/` (`SKILL.md` + `scripts/improve_recipes_tool.py`). Match its house style: in-pod helper with `gather`/`fetch-*`/`put-eval` subcommands, NDJSON on stdout, base64 eval payload as argv (stdin is consumed by the piped script), SELECT-only Postgres, S3 writes limited to the eval doc, versioned taxonomy in `SKILL.md`, "every diff hunk cites at least one episode id," three-section PR body (before/after aggregates, per-edit evidence rows, out-of-scope observed).
- **Migrations:** `projects/monolith/chart/migrations/YYYYMMDDHHMMSS_<name>.sql` + regenerate `atlas.sum`. Per repo memory, regenerate `atlas.sum` with CI's pinned Atlas (community v1.1.0), not a local newer Atlas. SQLite test fixtures use `SQLModel.metadata.create_all`, which does NOT see partial indexes or migration-only constraints, so mirror any `CHECK` in `__table_args__` and keep partial-unique indexes migration-only.
- **Async/session rule (`projects/monolith/CLAUDE.md`):** never call a sync `Session` method inside an `async def` on the bot loop. Do DB writes via `await asyncio.to_thread(_sync_helper, plain_data)`; the helper opens its own `Session(get_engine())` and commits. Never pass the caller's session into `to_thread`.

---

# PR 1 — Reaction persistence (monolith)

Branch: `feat/improve-ambient` (this worktree). Commit each task. Push once at the end of PR 1; CI is the test loop.

## Task 1: Migration — `chat.reaction_event` table

**Files:**
- Create: `projects/monolith/chart/migrations/20260705000000_chat_reaction_event.sql`
- Modify: `projects/monolith/chart/migrations/atlas.sum` (regenerate)

**Step 1: Write the migration**

```sql
-- Persist human reactions on Bosun's own messages in ambient channels, so the
-- /improve-ambient feedback loop has a ground-truth fluidity/productivity signal
-- (a 👍 vs 👎 vs 🤖 on a reply) instead of only inferring from follow-up text.
-- Only reactions whose reacted message was authored by the bot are stored
-- (target_is_bot is always true here); the column is kept explicit for clarity
-- and to leave room to widen the capture later. reactor_id is the human who
-- reacted (never the bot's own seed reactions). action distinguishes add/remove
-- so a later removal cancels an earlier positive/negative signal.
CREATE TABLE IF NOT EXISTS chat.reaction_event (
    id          BIGSERIAL PRIMARY KEY,
    channel_id  TEXT NOT NULL DEFAULT '',
    message_id  TEXT NOT NULL DEFAULT '',
    target_is_bot BOOLEAN NOT NULL DEFAULT TRUE,
    emoji       TEXT NOT NULL DEFAULT '',
    reactor_id  TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL DEFAULT 'add',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT reaction_event_action_valid CHECK (action IN ('add', 'remove'))
);

-- The skill queries reactions by the reacted message id (to attach them to the
-- episode whose reply is that message) and by recency window.
CREATE INDEX IF NOT EXISTS reaction_event_message_idx
    ON chat.reaction_event (message_id);
CREATE INDEX IF NOT EXISTS reaction_event_created_idx
    ON chat.reaction_event (created_at);
```

**Step 2: Regenerate atlas.sum**

Per repo convention, regenerate with CI's pinned Atlas (community v1.1.0). If Atlas is not available locally, note in the commit that `atlas.sum` must be regenerated by CI/hand-verified; do NOT hand-edit hashes blindly. Command shape:

Run: `atlas migrate hash --dir "file://projects/monolith/chart/migrations"`
Expected: `atlas.sum` gains the new file's hash, no other lines change.

**Step 3: Commit**

```bash
git add projects/monolith/chart/migrations/20260705000000_chat_reaction_event.sql projects/monolith/chart/migrations/atlas.sum
git commit -m "feat(chat): add reaction_event table for ambient reaction signal"
```

## Task 2: Model — `ReactionEvent`

**Files:**
- Modify: `projects/monolith/chat/models.py` (add class near `AttentionDecision`)

**Step 1: Add the SQLModel**

Mirror the `AttentionDecision` style (schema `chat`, `extend_existing`, `CheckConstraint` mirroring the migration so SQLite fixtures enforce it):

```python
class ReactionEvent(SQLModel, table=True):
    """Human reactions on Bosun's own messages in ambient channels (ADR chat/NNN).

    Ground-truth signal for the /improve-ambient loop: a 👍/👎/🤖 on a reply is
    a cheaper, clearer quality signal than inferring from follow-up text. Only
    reactions on bot-authored messages are persisted (target_is_bot always True
    today); the bot's own seed reactions are never logged. action add/remove
    lets a removal cancel an earlier signal.
    """

    __tablename__ = "reaction_event"
    __table_args__ = (
        CheckConstraint(
            "action IN ('add', 'remove')",
            name="reaction_event_action_valid",
        ),
        {"schema": "chat", "extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    channel_id: str = Field(default="")
    message_id: str = Field(default="")
    target_is_bot: bool = Field(default=True)
    emoji: str = Field(default="")
    reactor_id: str = Field(default="")
    action: str = Field(default="add")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
```

**Step 2: Commit**

```bash
git add projects/monolith/chat/models.py
git commit -m "feat(chat): add ReactionEvent model"
```

## Task 3: Persist reactions in the bot handlers

**Files:**
- Modify: `projects/monolith/chat/bot.py` (`on_raw_reaction_add` ~810; add `on_raw_reaction_remove`)

**Design:** Persist a reaction event when (a) the reactor is not the bot, and (b) the reacted message was authored by the bot (`payload.message_author_id == self.user.id`). Do this FIRST, before the existing proposal-confirm early-returns, so the proposal 👍/👎 are also captured (they are a signal too). Writes go through a sync helper on a worker thread per the monolith async/session rule.

**Step 1: Add a sync persistence helper** (module-level in bot.py or a small `chat/reactions.py`; keep it where the SQLite test can import it directly):

```python
def _record_reaction(data: dict) -> None:
    """Persist one human reaction on a bot message. Own session; safe in to_thread."""
    from app.db import get_engine
    from chat.models import ReactionEvent

    with Session(get_engine()) as session:
        session.add(ReactionEvent(**data))
        session.commit()
```

**Step 2: Call it from both handlers, best-effort:**

At the TOP of `on_raw_reaction_add` (after the `payload.user_id == self.user.id` guard that skips the bot's own reactions), and in a new `on_raw_reaction_remove`:

```python
    # Persist the reaction as a fluidity signal for /improve-ambient, but only
    # when the reacted message was Bosun's own (message_author_id is populated on
    # raw reaction events). Best-effort: a persistence failure must never block
    # the directive-confirm flow below.
    author_id = getattr(payload, "message_author_id", None)
    if author_id is not None and str(author_id) == str(self.user.id):
        try:
            await asyncio.to_thread(
                _record_reaction,
                {
                    "channel_id": str(payload.channel_id),
                    "message_id": str(payload.message_id),
                    "target_is_bot": True,
                    "emoji": str(payload.emoji),
                    "reactor_id": str(payload.user_id),
                    "action": "add",  # "remove" in on_raw_reaction_remove
                },
            )
        except Exception:
            logger.exception("reaction_event: persist failed (non-fatal)")
```

The new `on_raw_reaction_remove` is the same body with `action="remove"` and no proposal handling (removes never confirm a proposal).

**Step 3: Commit**

```bash
git add projects/monolith/chat/bot.py
git commit -m "feat(chat): persist human reactions on bot messages"
```

## Task 4: Tests for reaction persistence

**Files:**
- Modify: `projects/monolith/chat/bot_on_message_test.py` (near `TestDirectiveReactionHandler`)

**Step 1: Write tests** (SQLite fixture, `create_all`; assert on rows written):

- `test_reaction_on_bot_message_persisted`: raw add with `message_author_id == bot.user.id` and a human `user_id` → one `ReactionEvent(action='add', emoji=..., reactor_id=...)` row.
- `test_reaction_on_human_message_not_persisted`: `message_author_id` is some human id → zero rows.
- `test_bot_own_reaction_not_persisted`: `payload.user_id == bot.user.id` → zero rows (existing early return).
- `test_reaction_remove_persisted`: `on_raw_reaction_remove` on a bot message → one row with `action='remove'`.
- Keep the existing proposal-confirm assertions passing (persistence runs before, does not alter the 👍/👎 apply/discard behaviour).

**Step 2: Verify (via CI at end of PR)** — no local test loop. Note expected: all four new tests pass; `TestDirectiveReactionHandler` unchanged.

**Step 3: Commit**

```bash
git add projects/monolith/chat/bot_on_message_test.py
git commit -m "test(chat): cover reaction_event persistence"
```

## Task 5: Chart bump + push PR 1

**Files:**
- Modify: `projects/monolith/chart/Chart.yaml` + `projects/monolith/deploy/application.yaml` (via `bazel/tools/git/bump-chart.sh`)

**Step 1: Bump the chart** (a schema migration must deploy, so the bump is required in this PR):

Run: `bazel/tools/git/bump-chart.sh projects/monolith`

**Step 2: Format** (regenerates BUILD/gazelle for any new test targets — see the monolith pytest-target gotcha; a new `*_test.py` function in an existing file is fine, but a new file needs a hand-added `py_test`; this task only edits existing files):

Run: `bazel/tools/format/fast-format.sh`

**Step 3: Commit, push, open PR**

```bash
git add -A
git commit -m "build(monolith): bump chart for reaction_event"
git push -u origin feat/improve-ambient
gh pr create --title "feat(chat): persist ambient reactions for /improve-ambient" --body "<three-line body: what, why, links ADR>"
```

**Step 4: Watch CI**

Run: `gh pr checks <n> --watch`. On failure, fetch the log via `mcp__buildbuddy__get_invocation` (commitSha selector) and quote the assertion before hypothesizing. Rebase-merge when green.

---

# PR 2 — The `/improve-ambient` skill

Open a fresh worktree off updated `origin/main` after PR 1 merges (never reuse a merged branch): `git -C ~/repos/homelab worktree add -b feat/improve-ambient-skill /tmp/claude-worktrees/improve-ambient-skill origin/main`.

## Task 6: ADR — ambient feedback loop, three-level routing, and directive autopilot

**Files:**
- Create: `docs/decisions/chat/NNN-improve-ambient-loop.md` (next number in the chat category — `ls docs/decisions/chat/` first)

**Content (rationale, not implementation):** Why an offline evidence-backed skill (Opus judgement, reviewed PRs) for the code levers, plus a background autopilot for the reversible levers. The versioned taxonomy. The **three-level routing rule** (global prompt/gate edit vs channel directive vs personal style pref). The autopilot's safety argument: directives are the only levers whose quality signal (reactions + follow-up) returns within minutes, so an auto-applied directive can be **self-validated against its own downstream reaction delta and auto-reverted on regression** — which is why directives, and only directives, go autonomous while code levers stay human-reviewed. The **silent-background** decision (no channel announcements) and the **introspection surface** (expose directives + autopilot provenance out-of-band for manual tuning) instead. The `source` precedence rule (manual > autopilot). The `guard()` keyword constraint. Status: Accepted. Use the `adr` skill.

**Commit:** `docs(chat): ADR for ambient feedback loop and directive autopilot`

## Task 7: In-pod helper — `improve_ambient_tool.py`

**Files:**
- Create: `.claude/skills/improve-ambient/scripts/improve_ambient_tool.py`
- Create (if the sibling has one): `.claude/skills/improve-ambient/scripts/BUILD.bazel` — mirror `.claude/skills/improve-recipes/scripts/` exactly (check whether it has a BUILD target or is gazelle-excluded; replicate that).

**Subcommands (mirror `improve_recipes_tool.py`):**
- `gather --since <ISO8601>`: NDJSON, one record per **ambient activation episode**. An episode is a `chat.attention_decision` row with `decision='engage'`, enriched with: the message it engaged on (`chat.messages`), whether it became an agent run (`claude_agent.agent_threads` where `session_id`/`discord_thread` ties back, with `result`/`result_error`), any human follow-up in the channel within N minutes after the engage (count + whether the same author replied), and reactions on Bosun's reply (`chat.reaction_event` joined on the reply's `message_id`, aggregated to counts by emoji and net add/remove). Include BOTH `channel_id` and the engaged-message `author_id` on every record so the skill can cluster per-channel AND per-person (the three-level routing signal).
- `fetch-episode <episode_id>`: the full transcript slice around one episode (surrounding messages + the reply text + reactions + agent result), for deep-read scoring.
- `put-eval <episode_id> <b64>`: base64 eval JSON as argv[3] → `s3://artifacts/ambient-evals/<episode_id>.json`. SELECT-only Postgres; S3 writes limited to this key prefix.

**Guardrails (copy sibling):** Postgres SELECT only; S3 writes only under `ambient-evals/`; log best-effort failures to stderr (stdout is the NDJSON channel).

**eval JSON contract:**
```
{episode_id, channel_id, taxonomy_version, failure_modes: [{mode, confidence, rationale}],
 signals: {attention_confidence, became_agent, human_followups, same_author_replied,
           reactions: {"👍": n, "👎": n, ...}, net_reaction},
 route: "chat"|"agent", classified_at (ISO), levers_ref (git SHA of the lever files)}
```

**Commit:** `feat(skill): improve-ambient in-pod helper`

## Task 8: `SKILL.md`

**Files:**
- Create: `.claude/skills/improve-ambient/SKILL.md`

**Frontmatter** (match sibling voice; wire the trigger phrases):
```yaml
---
name: improve-ambient
description: >
  Fluidity + productivity feedback loop over ambient Discord chat activations:
  score each engagement (was it wanted, was the reply fluid, did it land) from
  attention decisions, reactions, and follow-up, then open evidence-backed PRs
  tuning the ambient system prompt, the agent-reply voice prompt, and the
  attention gate, and stage channel directives (or flag per-person style prefs)
  where behaviour must diverge. Use when asked to "improve the ambient chat",
  "/improve-ambient", "why did Bosun barge in / miss that", or "ambient feedback
  loop". This is the on-demand, human-reviewed half; the directive autopilot
  (chat/autopilot_job.py) is the autonomous background half. For goosecracker
  agent-run mechanics use improve-recipes; for artifact design use
  improve-artifacts.
---
```

**Body sections:**
1. **Unit of analysis:** the ambient activation episode (as gathered above).
2. **Taxonomy v1** (versioned; bump = a PR edit here). Two families:
   - *Engagement-timing:* `barged-in` (engaged unwanted), `over-eager` (too frequent for the channel), `missed-cue` (ignored a clear invite — cross-check sampled `decision='ignore'` rows + a following human nudge), `interrupted-thinking`.
   - *Reply-quality/outcome:* `third-person-leak` (said "the agent"/third person), `invented-link` (URL/PR/file not in the result), `wall-of-text` (padded/over-long), `off-voice` (not Bosun's register), `under-delivery` (didn't answer), `ignored-by-humans` (no follow-up, no reaction), `productive` (positive: reaction 👍 or the conversation continued).
3. **Lever routing table:**

   | Failure mode(s) | Lever | File / mechanism |
   |---|---|---|
   | voice, `wall-of-text`, `off-voice`, general verbosity — **uniform across channels + people** | ambient/base system prompt | `chat/agent.py` `build_system_prompt()` |
   | `third-person-leak`, `invented-link` on agent-delivered replies | agent-reply concierge prompt | `chat/summarizer.py` `_build_agent_reply_prompt()` |
   | `barged-in`, `over-eager`, `missed-cue` | attention gate prompt + thresholds | `chat/attention.py` `evaluate()` prompt, `ATTENTION_THRESHOLD`, `_RECENT_TAG_THRESHOLD` |
   | any failure **concentrated in one channel** | **channel directive** | `chat/directives.py` `propose_update()` — stage a proposal, do not edit code |
   | friction **tied to one person across channels** (they want terser/warmer/etc.) | **personal style pref** | `chat/directives.py` `set_style_pref()` — via the PR 3 autopilot path (no proposal flow exists); the skill flags it for the autopilot rather than setting it directly |

4. **Three-level routing rule (the "where is a divergence protocol required" answer):** cluster confirmed failures by `channel_id` AND `author_id`. (a) A mode across ≥2 channels at similar rates and not tied to one person → **global prompt/gate edit** (reviewed PR). (b) Concentrated in one channel, other channels fine → **channel directive**. (c) Tied to one person regardless of channel → **personal style pref**. State this rule explicitly; it is the skill's distinctive judgement, and it is exactly the classification the PR 3 autopilot consumes.
5. **Directive `guard()` gotcha:** proposed directive text must avoid the blocked keywords (`tool`, `grant`, `acl`, `permission`, `ambient`, `repo`, `admin`); phrase around them or `propose_update` silently rejects. Directives are staged (propose-then-confirm), never auto-applied — a human 👍 in Discord confirms.
6. **Methodology:** window defaults to "since the last commit touching the lever files" (`chat/agent.py`, `chat/summarizer.py`, `chat/attention.py`), overridable with `--since` or a single episode id. Gather → skip episodes already eval'd at the current `taxonomy_version` → deep-read the worst K (rank by a fluidity/productivity score: negative reactions and `barged-in`/`under-delivery` worst) → map to levers → edit lever files and/or stage directive proposals. Minimum 5 episodes in window or report-only (no PR). Reactions-on-bot are ground truth where present; before the `reaction_event` data has accrued, fall back to follow-up + attention-confidence proxies and say so in the report.
7. **Write scope:** PRs may edit `agent.py` / `summarizer.py` / `attention.py` (Joe reviews every PR) and stage `channel_directive` proposals. Per-person friction is **flagged for the autopilot** (recorded as a suggestion via the introspection surface), not set directly by the skill — there is no per-user proposal flow. Every diff hunk cites ≥1 episode id. Sanity-check: `python3 -c "import ast; ast.parse(open(f).read())"` on each edited .py before PR. Chart bump for the monolith in the same PR (prompt/gate edits deploy via the monolith image).
8. **PR body template** (three sections, sibling-identical): before/after aggregates (failure-mode histogram, negative-reaction rate, barge-in rate this window vs previous), per-edit evidence rows (episode_id, channel, signals, transcript excerpt, which diff line addresses it), out-of-scope observed.
9. **Guardrails:** read-only Postgres; S3 writes only `ambient-evals/`; skip infrastructure/env failures (never diff them); directive proposals only through `propose_update`.

**Commit:** `feat(skill): improve-ambient SKILL.md`

## Task 9: Register + verify the skill loads

**Files:**
- Modify: any skill index/registration the sibling skills use (check how `improve-recipes` is discovered — likely just its directory under `.claude/skills/`; if there is a manifest listing skills, add it there).

**Step 1:** Confirm `improve-recipes` needs no extra registration beyond its directory; replicate. Run `bazel/tools/format/fast-format.sh` (BUILD/gazelle for the new script if it is a bazel target).

**Step 2: Commit, push, open PR 2**

```bash
git add -A && git commit -m "feat(skill): wire /improve-ambient"
git push -u origin feat/improve-ambient-skill
gh pr create --title "feat(skill): add /improve-ambient feedback loop" --body "<what/why, links the ADR and PR 1>"
gh pr checks <n> --watch
```

## Task 10 (post-merge, optional first run): smoke the loop

After PR 2 merges, run `/improve-ambient` once in report-only mode over a recent window to confirm the helper gathers episodes and the taxonomy applies cleanly, before letting it open its first tuning PR. Note in the run whether reaction data has accrued yet.

---

---

# PR 3 — Directive autopilot (silent background loop + introspection surface)

Fresh worktree off updated `origin/main` after PR 2 merges: `feat/ambient-directive-autopilot`. This PR touches the public/observability-adjacent behaviour of the bot indirectly; read `docs/observability.md` only if wiring metrics. All levers here are monolith-side (single chart bump).

**Reused logic:** the episode gather + classification lives in monolith code (a `chat/ambient_analysis.py` core that both the CronWorkflow handler and, optionally, the skill's report can call). Classification uses the existing in-monolith model seam (`chat.summarizer.build_llm_caller` → Qwen), the same one `observer_job` uses. The Opus-driven deep-read stays in the skill (PR 2); the autopilot uses the cheap in-monolith classifier, gated hard on confidence + evidence.

## Task 11: Migration — `source` provenance + `chat.directive_autopilot` state

**Files:**
- Create: `projects/monolith/chart/migrations/20260705010000_directive_autopilot.sql`
- Modify: `projects/monolith/chart/migrations/atlas.sum` (regenerate, CI-pinned Atlas)

```sql
-- Provenance so a human manual tune wins over the autopilot (source precedence).
ALTER TABLE chat.channel_directive ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'seed';
ALTER TABLE chat.user_style_pref  ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'seed';

-- Autopilot decision + self-validation log. One row per autonomous action;
-- the introspection surface reads this to explain what changed and why, and the
-- validation phase reads pending_validation rows to keep/revert.
CREATE TABLE IF NOT EXISTS chat.directive_autopilot (
    id             BIGSERIAL PRIMARY KEY,
    scope_kind     TEXT NOT NULL DEFAULT 'channel',   -- 'channel' | 'user'
    scope_id       TEXT NOT NULL DEFAULT '',          -- channel_id or user_id
    target_version INTEGER NOT NULL DEFAULT 0,         -- directive/pref version applied
    prior_version  INTEGER,                            -- version to restore on revert
    baseline_json  TEXT NOT NULL DEFAULT '{}',         -- pre-apply score + components
    rationale      TEXT NOT NULL DEFAULT '',
    evidence_json  TEXT NOT NULL DEFAULT '[]',         -- supporting episode ids
    status         TEXT NOT NULL DEFAULT 'pending_validation',
    applied_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    validate_after TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT directive_autopilot_scope_valid CHECK (scope_kind IN ('channel', 'user')),
    CONSTRAINT directive_autopilot_status_valid CHECK (
        status IN ('pending_validation','kept','reverted','superseded_manual','proposed','suggested')
    )
);
CREATE INDEX IF NOT EXISTS directive_autopilot_pending_idx
    ON chat.directive_autopilot (status, validate_after);
```

**Commit:** `feat(chat): add directive source provenance and autopilot state`

## Task 12: Models — `source` fields + `DirectiveAutopilot`

**Files:** Modify `projects/monolith/chat/models.py` — add `source: str = Field(default="seed")` to `ChannelDirective` and `UserStylePref`; add a `DirectiveAutopilot` SQLModel mirroring the table (with `CheckConstraint`s mirrored for SQLite fixtures).

**Commit:** `feat(chat): DirectiveAutopilot model + source fields`

## Task 13: Autopilot core + CronWorkflow handler

**Files:**
- Create: `projects/monolith/chat/ambient_analysis.py` — sync core: `gather_episodes(session, since)`, `score_window(session, scope_kind, scope_id, since, until)` → composite fluidity/productivity score, `classify_scope(...)` (LLM via injected caller) → `{target_kind, proposed_text, confidence, evidence_ids, rationale}`.
- Create: `projects/monolith/chat/autopilot_job.py` — async handler registered as a job, following `observer_job.py` exactly (network/LLM with `await`, all Session I/O via `await asyncio.to_thread(_sync_helper, plain_data)` per `projects/monolith/CLAUDE.md`).

**Handler shape (two phases per run):**

1. **Validate phase (first):** for `directive_autopilot` rows `status='pending_validation'` with `now >= validate_after`: recompute `score_window` over `[applied_at, now]`. If the active row for the scope is now `source='manual'` → `status='superseded_manual'`. Else if `post < baseline - AUTOPILOT_REGRESS_MARGIN` → **revert** (reinstate `prior_version` as active) and `status='reverted'`; otherwise `status='kept'`.
2. **Apply phase:** `gather_episodes` since last run → cluster per `channel_id` and per `author_id` → `classify_scope` each cluster with enough evidence. **Apply gate (ALL must hold):** `confidence ≥ AUTOPILOT_MIN_CONFIDENCE` (0.8), `distinct evidence episodes ≥ AUTOPILOT_MIN_EVIDENCE` (3), `guard(proposed_text)` passes, active directive not `source='manual'` within `AUTOPILOT_MANUAL_COOLDOWN_DAYS`, scope not in autopilot cooldown `AUTOPILOT_COOLDOWN_DAYS` (7), and the change is a bounded refinement (length delta within a cap), not a rewrite. On pass → **apply silently**: channel → insert active `channel_directive` `source='autopilot'`; user → `set_style_pref(..., source='autopilot')`; record a `directive_autopilot` row (baseline = pre-apply score, `validate_after = now + AUTOPILOT_VALIDATE_DAYS` (3)). On fail-but-confident → channel: `propose_update()` (👍 flow), `status='proposed'`; user: log `status='suggested'` (no proposal flow exists) for the introspection surface. **No channel messages at any point.**

All thresholds are module constants, env-overridable (`AUTOPILOT_*`). Fail-closed: any classify/DB error logs and skips that scope, never applies.

**Commit:** `feat(chat): directive autopilot job with self-validating revert`

## Task 14: Introspection MCP surface (out-of-band tuning)

**Files:** Modify `projects/monolith/agent/mcp.py` (or the chat MCP module the sibling tools live in) — add owner-gated tools. Keep descriptions **plain text with no backticks** (Context Forge drops MCP tools whose description contains a backtick — see repo memory).

- `monolith-chat-list-directives` — active channel directives + user style prefs, each with source, version, and its most recent autopilot action (status + rationale).
- `monolith-chat-directive-history` (scope_kind, scope_id) — version history + `directive_autopilot` log for that scope.
- `monolith-chat-set-directive` (scope_kind, scope_id, text) — manual set, `source='manual'` (guarded); channel = direct active insert, user = `set_style_pref`.
- `monolith-chat-pin-directive` (scope_kind, scope_id) — mark the active row `source='manual'` so the autopilot leaves it alone.
- `monolith-chat-revert-directive` (scope_kind, scope_id) — restore the prior version manually.

**After merge:** run `/refresh-context-forge-tools` so Context Forge re-discovers the new tools (it never auto-refreshes — repo memory).

**Commit:** `feat(chat): MCP surface to introspect and tune directives`

## Task 15: Tests

**Files:** Create `projects/monolith/chat/autopilot_job_test.py` (+ analysis core tests). SQLite `create_all`. Cover: `score_window` arithmetic on fixture reactions/decisions; apply gate passes only when all conditions hold; apply writes the active row + a `pending_validation` log row silently (no outbox post); validate phase reverts on regression and keeps on improvement; `source='manual'` blocks autopilot (precedence) and yields `superseded_manual`; `guard`-blocked text is never applied; per-user suggestion path logs `suggested` and does not set a pref. New test file → hand-add the `py_test` target (monolith gazelle gotcha, repo memory).

**Commit:** `test(chat): cover directive autopilot gate, revert, precedence`

## Task 16: Cron wiring + chart bump + push PR 3

**Files:** Modify `projects/monolith/chart/values.yaml` (add `chat-directive-autopilot` Argo CronWorkflow, mirroring `chat-observe-directives`; daily schedule; follow the v4 schedules-LIST wiring gotcha in repo memory) + register the job handler wherever `observer_job` is registered. Then `bazel/tools/git/bump-chart.sh projects/monolith`, `bazel/tools/format/fast-format.sh`, commit, push, `gh pr create`, `gh pr checks <n> --watch`, rebase-merge on green.

**Commit:** `feat(chat): schedule directive autopilot cronworkflow` then `build(monolith): bump chart for autopilot`

---

## Definition of done

- PR 1: `chat.reaction_event` live; human reactions on Bosun's ambient replies persist (add + remove); tests green on CI; chart bumped and rolled out.
- PR 2: `/improve-ambient` skill loads; helper gathers episodes read-only (per channel + per person); SKILL.md carries taxonomy v1 + lever routing + the three-level (global / channel / personal) rule; ADR recorded.
- PR 3: directive autopilot runs silently on a schedule; applies high-confidence low-risk channel + personal directive updates, baselines, and auto-reverts on regression; manual tuning wins via `source` precedence; MCP introspection surface lists/edits/pins/reverts directives; no channel announcements anywhere.
- One comprehensive Opus code review per PR before merge (not per task).
- No em-dashes anywhere. Conventional Commits throughout. Rebase-merge only.
