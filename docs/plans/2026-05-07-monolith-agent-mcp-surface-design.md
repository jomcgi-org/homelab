# Monolith Agent MCP Surface — Design

## Summary

Add a `monolith-agent-*` tool family to the existing monolith MCP server so cloud Claude Code Routines (the `claude-routine-agent` actor) can do useful homelab work. Three additions: an `agent_locks` table for opportunistic TTL locks, a `routine_jobs` table for scheduled work delegated to Routines, and a thin tool layer (16 tools) over both, plus the existing in-cluster scheduler, knowledge ingest, SigNoz alerts, and Discord bot.

No new service, no new image, no new ArgoCD app — handlers ship inside the existing `monolith` deployment.

## Goals

- Let cloud Routines pick up and complete delegated work without two Routines duplicating each other.
- Let cloud Routines observe internal cluster state that isn't visible from outside (scheduler, knowledge ingest, alerts).
- Let cloud Routines notify the operator via Discord.
- Keep the existing in-cluster scheduler (`shared/scheduler.py`) and its handlers (`gardener`, `calendar-poll`, `vault-backup`, etc.) **completely unchanged**.

## Non-goals

- A general work-queue with retries, dead-lettering, fairness, priorities. Routines that fail simply don't `complete-routine-job`; the lock TTL expires and the row becomes claimable again. Failure analysis is out of scope for v1.
- A GitHub proxy. Cloud Routines have their own `gh` and a repo checkout — they don't need monolith to forward GH calls.
- Replacing `agent-orchestrator` (NATS KV-backed one-off jobs). Different domain — orchestrator is for in-cluster job execution; routine_jobs is for delegating work to external Routines.

## Decisions

- **Namespace:** all new tools prefixed `monolith-agent-`. Routines reference them as `mcp__claude_ai_homelab__monolith-agent-*`.
- **Three tables:** `scheduled_jobs` (existing, untouched), `routine_jobs` (new, delegated work), `agent_locks` (new, opportunistic locks). No polymorphism or shared rows.
- **Routine identity:** `claude-routine-agent` is the conceptual actor. The cluster does not run Routine code; it serves the data the Routine needs. The `routine_jobs` table has no `handler` column because there's only one logical handler — the cloud Routine pool.
- **Discord transport:** in-process call to `chat.bot.send_message()` from the MCP tool. **No NATS** — the bot lives in the same Python process as the MCP server, so a NATS publish would be a needless serialization round-trip.
- **Discord defaults:** server `1501965852042330302`, channel `1501965852969402517` baked into Helm values. `channel` arg optional; if specified, validated against an allow-list.
- **Reuse:** tool handlers are thin wrappers — they call existing public functions (`Scheduler.list_jobs`, `KnowledgeStore.list_dead_letters`, etc.) rather than reimplementing filters.
- **Opt-in default for external discovery:** Routines only act on GitHub items explicitly labeled `claude`. Untagged items are off-limits. Enforcement lives in Routine prompts (the `gh` discovery query), not in the cluster. See "Opt-in conventions" below.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│ scheduled_jobs  (EXISTING — UNCHANGED)                              │
│   In-cluster cron. Owned by shared/scheduler.py tick loop.          │
│   Handlers: gardener, calendar-poll, vault-backup, ...              │
└─────────────────────────────────────────────────────────────────────┘
                              ↑ (read by check-stuck-jobs,
                              │  check-orphan-jobs, trigger-job)
┌─────────────────────────────────────────────────────────────────────┐
│ monolith MCP server (FastMCP, in-process with Discord bot)          │
│   Existing knowledge/task tools  +  new monolith-agent-* tools      │
└─────────────────────────────────────────────────────────────────────┘
                              ↓ (write/read)
┌─────────────────────────────────────────────────────────────────────┐
│ routine_jobs    (NEW)                                               │
│   Delegated work claimed and run by cloud Routines.                 │
└─────────────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────────────┐
│ agent_locks     (NEW)                                               │
│   Opportunistic TTL locks for ad-hoc dedup (any key).               │
└─────────────────────────────────────────────────────────────────────┘
                              ↑
┌─────────────────────────────────────────────────────────────────────┐
│ Cloud Routines (claude-routine-agent)                               │
│   Run on Claude Code cron. Connect to monolith MCP via the          │
│   account-scoped claude_ai_homelab connector.                       │
└─────────────────────────────────────────────────────────────────────┘
```

The in-cluster scheduler is **isolated** from `routine_jobs`. No filter changes, no new handler registrations, no risk that a future scheduler refactor breaks Routine semantics.

## Schema

### `routine_jobs` (new)

```sql
CREATE TABLE routine_jobs (
    name             TEXT PRIMARY KEY,
    routine_kind     TEXT NOT NULL,          -- e.g. 'check', 'register', 'pr-fix', 'gh-triage'
    interval_secs    INTEGER,                -- NULL = one-off
    next_run_at      TIMESTAMPTZ,            -- when this row becomes claimable
    last_run_at      TIMESTAMPTZ,
    last_status      TEXT,                   -- 'ok' | 'error: {message}'
    last_summary     TEXT,                   -- free-form result summary from Routine
    locked_by        TEXT,                   -- holder string from claim
    locked_at        TIMESTAMPTZ,
    ttl_secs         INTEGER,                -- lock TTL set at claim time
    payload          JSONB,                  -- free-form instructions for the Routine
    created_by       TEXT,                   -- who registered (e.g. 'human:joe', 'routine:register-jobs')
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_routine_jobs_due
    ON routine_jobs (next_run_at)
    WHERE locked_by IS NULL;

CREATE INDEX idx_routine_jobs_kind
    ON routine_jobs (routine_kind);
```

**Claim query** (mirrors `scheduled_jobs` SKIP LOCKED idiom):

```sql
SELECT * FROM routine_jobs
WHERE next_run_at <= now()
  AND (
    locked_by IS NULL
    OR locked_at + (ttl_secs || ' seconds')::interval < now()
  )
  AND ($1::text IS NULL OR routine_kind = $1)
ORDER BY next_run_at ASC
LIMIT 1
FOR UPDATE SKIP LOCKED;
```

**On complete:** `last_run_at = now()`, `last_status` and `last_summary` set, `locked_by`/`locked_at` cleared. If `interval_secs` is non-null, `next_run_at = now() + interval_secs * '1 second'::interval`; otherwise the row sits with `next_run_at` unchanged (typically NULL for one-offs) and won't re-fire until manually triggered or deregistered.

### `agent_locks` (new)

```sql
CREATE TABLE agent_locks (
    key          TEXT PRIMARY KEY,
    holder       TEXT NOT NULL,
    acquired_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    lock_id      UUID NOT NULL DEFAULT gen_random_uuid()
);

CREATE INDEX idx_agent_locks_expires
    ON agent_locks (expires_at);
```

`lock_id` is opaque to callers; `extend-lock` and `release-lock` use it to prevent a stale holder from operating on a lock that's been re-acquired by someone else.

**Acquire:**

```sql
INSERT INTO agent_locks (key, holder, expires_at)
VALUES ($1, $2, now() + ($3 || ' seconds')::interval)
ON CONFLICT (key) DO UPDATE
    SET holder = EXCLUDED.holder,
        acquired_at = now(),
        expires_at = EXCLUDED.expires_at,
        lock_id = gen_random_uuid()
    WHERE agent_locks.expires_at < now()  -- only steal expired locks
RETURNING lock_id, expires_at, (xmax = 0 OR agent_locks.lock_id = lock_id) AS acquired;
```

If the row exists and is unexpired, the `WHERE` blocks the update and `acquired = false`. The Routine retries later or moves on.

## Tools

### Locks (4)

| Tool                          | Args                        | Returns                                    |
| ----------------------------- | --------------------------- | ------------------------------------------ |
| `monolith-agent-acquire-lock` | `key`, `holder`, `ttl_secs` | `{acquired: bool, lock_id?, expires_at?}`  |
| `monolith-agent-extend-lock`  | `lock_id`, `ttl_secs`       | `{ok: bool, new_expires_at?}`              |
| `monolith-agent-release-lock` | `lock_id`                   | `{ok: bool}`                               |
| `monolith-agent-list-locks`   | `prefix?`                   | `[{key, holder, acquired_at, expires_at}]` |

### Notify (1)

| Tool                    | Args                                             | Returns         |
| ----------------------- | ------------------------------------------------ | --------------- |
| `monolith-agent-notify` | `message`, `level=info\|warn\|error`, `channel?` | `{ok, channel}` |

`level` controls the message prefix (`""`, `"⚠️ "`, `"🔴 "`). `channel` defaults to the Helm-configured value; if set, must be in the allow-list.

### Check (4)

| Tool                                 | Args                | Returns                                                |
| ------------------------------------ | ------------------- | ------------------------------------------------------ |
| `monolith-agent-check-stuck-jobs`    | `threshold_mins=10` | `[{name, handler, locked_by, locked_at, last_status}]` |
| `monolith-agent-check-orphan-jobs`   | —                   | `[{name, handler}]` (handler not in registry)          |
| `monolith-agent-check-dead-letters`  | `limit=20`          | `[{id, source, error, dead_lettered_at}]`              |
| `monolith-agent-check-firing-alerts` | —                   | `[{name, severity, fired_at, summary}]`                |

All four are pure reads. They reuse existing internal functions:

- `check-stuck-jobs` / `check-orphan-jobs` → `Scheduler.list_jobs` filtered
- `check-dead-letters` → existing query in `KnowledgeStore` used by the `debug-knowledge-ingest` skill
- `check-firing-alerts` → port the SigNoz `/api/v1/rules` call from `cluster_agents/collector_alerts.go` to a Python helper

### Trigger in-cluster job (1)

| Tool                         | Args   | Returns                                            |
| ---------------------------- | ------ | -------------------------------------------------- |
| `monolith-agent-trigger-job` | `name` | `{ok}` (sets `scheduled_jobs.next_run_at = now()`) |

Operates on `scheduled_jobs` only. To trigger a routine job, see `trigger-routine-job` below.

### Routine jobs (6)

| Tool                                    | Args                                                         | Returns                                                                                                                   |
| --------------------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------- |
| `monolith-agent-list-routine-jobs`      | `due_only=False`, `kind?=None`                               | `[{name, kind, next_run_at, last_run_at, last_status, locked_by, payload}]`                                               |
| `monolith-agent-claim-routine-job`      | `holder`, `ttl_secs`, `kind?=None`, `name?=None`             | `{claimed: bool, job?}` — if `name` set, claim that specific row; else claim next due, optionally filtered by `kind`      |
| `monolith-agent-complete-routine-job`   | `name`, `status`, `summary?`                                 | `{ok}` — sets `last_run_at`/`last_status`/`last_summary`, clears lock, advances `next_run_at` if `interval_secs` non-null |
| `monolith-agent-register-routine-job`   | `name`, `kind`, `interval_secs?`, `payload?`, `next_run_at?` | `{ok}` — error if `name` exists                                                                                           |
| `monolith-agent-deregister-routine-job` | `name`                                                       | `{ok}`                                                                                                                    |
| `monolith-agent-trigger-routine-job`    | `name`                                                       | `{ok}` (sets `routine_jobs.next_run_at = now()`)                                                                          |

`status` convention matches the existing scheduler: `"ok"` or `"error: {message}"`.

## Scenario coverage

| Scenario (from user)                | Tools used                                                                                                                                                           | Notes                                                     |
| ----------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| 1. Discord notify                   | `notify`                                                                                                                                                             | Default channel, no other args needed for the common case |
| 2. Trigger stuck job                | `check-stuck-jobs` → `trigger-job`                                                                                                                                   | Filter by `threshold_mins`; trigger by name               |
| 3. Detect failed/stuck/wrong        | `check-stuck-jobs`, `check-orphan-jobs`, `check-dead-letters`, `check-firing-alerts`                                                                                 | Each reads a different cluster-internal source            |
| 4. Resolve PR CI                    | `acquire-lock(key="pr-fix-{n}")` for dedup; cloud Routine does the fix in its session via BuildBuddy MCP + `gh` + repo checkout; `notify` for status; `release-lock` | Monolith stays out of the actual fix                      |
| 5. Pick up unattended GH issues/PRs | Either: register `routine_jobs` rows (one per issue, scheduled for inspection); or opportunistic `acquire-lock(key="gh-issue-{n}")` from a discovery Routine         | Both shapes supported                                     |

## Opt-in conventions for Routine-eligible work

Two paths exist for a Routine to find work:

1. **Internal (`routine_jobs` table).** Implicitly opt-in: rows exist only because someone registered them. No labels needed.
2. **External (GitHub discovery via `gh`).** Explicitly opt-in via labels.

For path 2, the convention is:

| Label     | Meaning                                            | Routine behavior                               |
| --------- | -------------------------------------------------- | ---------------------------------------------- |
| `claude`  | "AI Routine may pick this up"                      | **Required** — without this, Routine must skip |
| `manual`  | "Humans only — even if other labels say otherwise" | Hard skip; takes precedence over `claude`      |
| (neither) | Default                                            | Skip — opt-in only                             |

**Routine discovery queries always include the opt-in filter and the opt-out exclusion:**

```bash
gh pr list --label claude --state open \
  --json number,title,labels \
  --jq '.[] | select(.labels | map(.name) | contains(["manual"]) | not)'

gh issue list --label claude --state open \
  --json number,title,labels \
  --jq '.[] | select(.labels | map(.name) | contains(["manual"]) | not)'
```

**Why both labels** (and not just `claude`): the `manual` label gives a hard-stop way to freeze a PR mid-flight without removing the original eligibility marker. Useful when something was Claude-eligible but you've taken it over and don't want a Routine to grab it back when you push a new commit.

**No server-side enforcement.** The monolith MCP doesn't know about GH labels and doesn't proxy GH calls. Conventions live in Routine prompts; if a Routine prompt is wrong, the worst it can do is read public PR data — it can't act on a PR without first opening a fix branch / commenting, which is gated by the GH token's scopes.

**Optional follow-up label** (deferred to v2): `claude-routine-attempted` added by a Routine after it tries (success or fail), so subsequent Routines can see prior attempts and humans can audit. Trivial to add but not needed for v1.

## Routine workflow examples

### A "check-jobs" Routine (every 30m)

```
1. monolith-agent-list-routine-jobs(due_only=true, kind="check") → list
2. If empty, exit.
3. monolith-agent-claim-routine-job(holder="check-jobs:{run_id}", ttl_secs=1500, kind="check")
4. Run the work described in payload (each kind has its own payload schema).
5. monolith-agent-complete-routine-job(name=claim.name, status="ok"|"error: ...", summary=...)
6. (Optional) monolith-agent-notify(channel=default, level="info", message="check-jobs finished: {summary}")
```

### A "find-stale-prs" Routine (every 6h)

```
1. (in cloud session) gh pr list --label claude --state open --json number,title,labels \
     --jq '.[] | select(.labels | map(.name) | contains(["manual"]) | not)'
   # Opt-in filter: only PRs explicitly tagged `claude`, excluding any also tagged `manual`.
2. For each PR:
     monolith-agent-acquire-lock(key=f"pr-triage-{number}", holder="find-stale-prs:{run_id}", ttl_secs=600)
     If acquired:
       Investigate (read PR, recent commits, checks).
       If actionable: register a follow-up routine_job (kind="pr-fix", payload={pr, reason}).
       monolith-agent-release-lock(lock_id)
     Else: skip.
3. monolith-agent-notify(message=f"Triaged {N} eligible PRs, queued {M} fixes")
```

The first Routine is _internal-driven_ (scheduler row → claim → run). The second is _external-driven_ (Routine discovers work via `gh`, uses opportunistic locks for dedup, optionally writes scheduler rows for follow-up). Both flows are supported by the same surface.

## Security

- **Auth:** the monolith MCP is account-scoped via Claude.ai's `claude_ai_homelab` connector. Anyone authenticated to that account can call any tool. For a single-operator homelab this is fine; for shared use, future work would scope tools by API key claims.
- **Discord channel allow-list:** prevents a compromised Routine prompt from posting to arbitrary servers/channels. Default channel is the operator's private homelab server (1 person).
- **Write scope of agent surface:** the agent tools can register/deregister `routine_jobs` rows, take locks, trigger jobs, and post Discord — all reversible. They cannot modify `scheduled_jobs` rows beyond setting `next_run_at` (no insert, no delete, no payload change), so a misbehaving Routine cannot disable in-cluster cron handlers.
- **Audit:** every write tool logs caller (best-effort from MCP context), `name`/`key`/`lock_id`, and timestamp at INFO level. The existing monolith logging pipeline carries this to SigNoz.
- **GitHub opt-in:** see "Opt-in conventions" above. Default-off for untagged items prevents a Routine prompt error from acting on PRs/issues you didn't intend.
- **Lock-key conventions (recommended, not enforced):**
  - `pr-fix-{number}`, `pr-triage-{number}` — PR-scoped work
  - `gh-issue-{number}` — issue-scoped work
  - `stuck-job-{name}` — kicking a specific stuck scheduler job
  - `routine-job-{name}` — held automatically during routine_jobs claim (callers shouldn't use this prefix manually)

## Configuration

Helm values (`projects/monolith/deploy/values.yaml`):

```yaml
agent:
  discord:
    defaultServerId: "1501965852042330302"
    defaultChannelId: "1501965852969402517"
    allowedChannelIds:
      - "1501965852969402517"
```

Translated to env vars in the deployment:

```
MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID
MONOLITH_AGENT_DISCORD_DEFAULT_CHANNEL_ID
MONOLITH_AGENT_DISCORD_ALLOWED_CHANNEL_IDS  # comma-separated
```

Read into a `Settings` object in `projects/monolith/agent/config.py`.

## Module layout

```
projects/monolith/agent/
├── __init__.py
├── config.py          # Settings (env-driven Discord defaults + allow-list)
├── locks.py           # acquire/extend/release/list — agent_locks table operations
├── routine_jobs.py    # list/claim/complete/register/deregister/trigger — routine_jobs operations
├── checks.py          # check-stuck-jobs / orphan / dead-letters / firing-alerts (delegates to existing modules)
├── notify.py          # send-to-bot wrapper (calls chat.bot.send_message)
└── mcp.py             # @mcp.tool registrations — thin wrappers, no business logic
```

Migrations go in the existing monolith Alembic chain (one revision adds both new tables).

## Future: self-improving loop (v2)

The architecture leaves a clean path for a self-improving feedback loop:

```
Routine A fails on a task because it lacks capability X
       ↓
Routine A reports failure with a structured "blocking-gap" marker
       ↓
A "register-jobs" Routine scans recent failures, dedupes gaps, opens GH
issues (label `capability-gap`) prioritized by how many jobs each gap blocks
       ↓
An "implementer" Routine picks the highest-fan-out gap, opens a PR
implementing the missing capability, normal CI + human PR review
       ↓
Once merged, Routine A's next scheduled run succeeds (no longer blocked)
```

**Scope of "capability."** The implementer Routine isn't restricted to monolith MCP tools — a capability gap can be **any change the homelab repo can encode**. Examples:

| Gap kind            | Example gap name                     | What the implementer does                                         |
| ------------------- | ------------------------------------ | ----------------------------------------------------------------- |
| `mcp-tool`          | `monolith-agent-list-recent-deploys` | Add a new `@mcp.tool` handler in `projects/monolith/agent/mcp.py` |
| `scheduler-handler` | `cleanup-orphan-build-cache`         | Add Python handler + scheduler row in `projects/monolith/`        |
| `k8s-manifest`      | `imageupdater-yaml-for-service-x`    | Create `projects/<service>/deploy/imageupdater.yaml`              |
| `helm-values`       | `enable-otel-on-service-y`           | Edit `projects/<service>/deploy/values.yaml`                      |
| `network-policy`    | `allow-monolith-egress-to-grafana`   | Add a manifest (respecting the Linkerd NetworkPolicy memory)      |
| `bazel-target`      | `add-py3-image-for-z`                | Add BUILD file entries                                            |
| `semgrep-rule`      | `forbid-direct-prod-db-writes`       | Add rule under `bazel/semgrep/`                                   |
| `adr`               | `decision-on-routine-priority`       | Create `docs/decisions/<category>/NN-<topic>.md`                  |

The implementer Routine has full read/write access to the cloned repo (via cloud Claude Code's `--remote` bundle or git clone), can run `format`, and opens a PR via `gh`. **No new in-cluster surface is needed for v2** — the v1 tools plus the cloud Routine's existing repo + GH access are sufficient.

**v1 recording hook (zero schema change).** Routines that fail due to a missing capability use a free-form convention in their `complete-routine-job` summary:

```
status:  "error: missing capability"
summary: "BLOCKING-GAP[kind=mcp-tool]: monolith-agent-foo-tool — needed to enumerate X for kind=pr-fix"
```

The `BLOCKING-GAP[kind=...]:` prefix is the only convention v1 ships. The optional `[kind=...]` lets the implementer branch on which subsystem to edit (a `kind=mcp-tool` gap is a Python edit; a `kind=k8s-manifest` gap is a YAML edit). v2 will promote both to structured fields; until then, the convention is parseable and forward-compatible.

**v2 deferred work (not in this design):**

- **Capability-gap registry** — either a new column on `routine_jobs` or a sibling table tracking `(gap_name, blocked_job_name)` relationships.
- **Fan-out priority calculation** — `ORDER BY count(distinct blocked_job_name) DESC`. Plus a `priority` field on `routine_jobs` rows so a single blocked critical job can outrank 10 blocked trivial ones.
- **Implementer Routine** — a `claude-routine-agent` Routine of `kind="capability-gap"` whose prompt is "pick the highest-priority open capability gap, implement it as a PR against this repo, never auto-merge — wait for CI + human review."
- **GH issue mirror** — capability gaps mirror to GH issues (label `capability-gap`) so humans can see, comment, deprioritize, or close as wontfix.

**Hard constraints v2 must respect:**

- **No auto-merge for capability-gap PRs.** Always go through normal review. The loop is "self-proposing," not "self-merging." A buggy implementer can suggest bad fixes; CI + your review catches them.
- **Depth limit on transitive gaps.** If implementing gap A produces gap B, and implementing B produces gap C, stop after N hops (e.g. 2). Otherwise an implementer Routine could chase its own tail.
- **Human-readable gap names.** No UUIDs or hashes — gap identifiers should be the kind of string a human can understand at a glance (`missing-bb-mcp-cache-stats`, not `gap-7f3e2`).

### Improving the loop: tiered models + iterative review (v2+)

Single-pass implementation is wasteful at one end and risky at the other. A small agent pipeline with cost-tiered roles fixes both:

| Role                                             | Model default | Why                                                                    |
| ------------------------------------------------ | ------------- | ---------------------------------------------------------------------- |
| Discovery / triage (`check-*`, `find-stale-prs`) | Haiku         | Mechanical querying, simple decisions                                  |
| Implementer (drafts capability-gap PRs)          | Sonnet        | Pattern-matching from existing code; bulk edits, low risk per edit     |
| PR reviewer bot                                  | Opus          | Judgment work — catches subtle bugs, concurrency issues, security gaps |
| Reviser (addresses reviewer feedback)            | Sonnet        | Same as implementer — structured edits guided by review comments       |

**Constraint:** the reviewer must be a **different Routine instance** from the implementer, even on the same model. A single context that drafts-then-reviews rationalizes its own work; separation forces independent assessment.

**State machine via PR labels** (no new in-cluster surface — pure GH label conventions):

```
Gap registered (routine_jobs row, kind="capability-gap")
        ↓ implementer Routine claims, drafts PR
PR opened with labels:  capability-gap, needs-bot-review, iteration:0
        ↓ reviewer Routine fires (queries for needs-bot-review)
Reviewer posts verdict:
  • APPROVE         → label: bot-approved-needs-human   → notify operator
  • REQUEST_CHANGES → label: bot-requested-changes      → reviser fires
  • CLOSE           → label: bot-closed-wontfix         → comments rationale, closes PR
        ↓ reviser Routine (if REQUEST_CHANGES and iteration < N)
PR updated, label flipped back to needs-bot-review, iteration:k+1
        ↺ loop, max N iterations (default 3)
At iteration == N:  label adds max-iterations-reached, notify operator
```

**Routine `kind` values added in v2+:** `implementer`, `reviewer`, `reviser`. Each is a separate cloud Routine with its own cron schedule and prompt. Suggested cadences: implementer hourly, reviewer every 10 min, reviser every 15 min. None are real-time — review latency in tens of minutes is fine for capability gaps.

**Hooks the cluster might add for v2+:**

- `attempts` column on `routine_jobs` — counts how many times the gap has been picked up. Lets a "register-jobs" Routine deprioritize gaps that keep failing review.
- Optional: a small SQL view `gap_outcomes` that aggregates merged vs closed-as-wontfix PRs by `gap_kind`, so the operator can see which kinds the loop handles well.

Both are deferrable — the v1 surface plus PR labels are sufficient to run the loop without them.

**Failure modes to engineer against from day one:**

- **Sycophancy.** Reviewer rubber-stamps because the diff "looks structurally fine." Mitigations: reviewer prompt explicitly lists categories to challenge (concurrency, security, error handling, hidden assumptions); reviewer must cite specific lines in REQUEST_CHANGES verdicts; periodic human spot-check of `bot-approved-needs-human` PRs against actual quality.
- **Death spiral.** Revise → re-review → revise forever. Hard cap at iteration 3, then human-or-close. Also: reviser prompt instructed to push back if reviewer feedback is wrong, not just comply.
- **Cost runaway.** Sonnet drafting + Opus reviewing + Sonnet revising × 3 iterations × N gaps/day can be expensive. Per-PR token budget enforced by the implementer/reviser prompts (each iteration's diff size has a cap). If a gap consistently exceeds budget, mark it `requires-human` and stop.
- **Cross-contamination.** Two Routines simultaneously implementing related gaps could conflict. The opportunistic `agent_locks` surface from v1 covers this — implementer takes a lock per gap, scoped to the duration of the PR draft session.

**Open design questions for v2+ (not blocking v1):**

1. **Cost budget mechanism.** Per-PR token cap? Per-day total? How does the implementer Routine know its own remaining budget?
2. **Reviewer quality calibration.** How do we tell over time if Opus is being too lenient or too strict? Need a feedback signal (e.g. % of bot-approved PRs that the human then closes vs merges).
3. **Cross-gap dependencies.** If gap A and gap B both need to be implemented for a Routine to unblock, does the implementer batch them into one PR or sequence?

Why defer: the priority machinery (fan-out, depth limits, kind-based filtering) is much easier to design from real failure data than from imagined examples. v1 captures the data; v2 designs the consumer when we have actual gaps to look at.

## Out of scope for v1

- Retry/backoff for failed routine jobs (Routine itself decides whether to retry, by re-running on its next cron tick or registering a follow-up row).
- Per-Routine concurrency limits (the lock TTL provides this implicitly: only one holder at a time per row).
- Dead-letter queue for routine_jobs (a row that keeps timing out can be inspected via `list-routine-jobs` and deregistered manually).
- Webhook → routine_job bridge (e.g. GH webhook auto-creates a `pr-fix` row). Easy follow-up once we know what shape Routines actually want.
- Stuck-routine-job sweep tool (e.g. `check-stuck-routine-jobs(threshold_mins=...)`). Trivial to add when first needed.
- Rich Discord payloads (embeds, mentions, threads). Plain text covers all five scenarios; richer comes when something asks for it.

## Open questions

1. **`payload` shape per `routine_kind`.** Should we document/version the JSON schema for each kind (e.g. `pr-fix` payload = `{pr_number, repo, failing_targets[]}`)? Probably yes once a second kind exists; for v1 leave as free-form `JSONB` and let the first few rows establish convention.
2. **`created_by` provenance.** Want to record the MCP caller identity, but FastMCP doesn't surface request auth context to handlers consistently. v1: `created_by` is a free-form string the caller supplies (default `"unknown"`). Tighten later if account-scoped auth becomes available.
3. **Idempotency of `register-routine-job`.** Currently errors on duplicate name. Should we add an `upsert: bool = False` flag? Defer until first need.
4. **Opt-in label name.** The doc proposes `claude` (eligible) and `manual` (hard skip). Alternatives considered: `claude-routine` / `ai` / `agent-ok`. `claude` is short, matches the operator's mental model, and is unlikely to collide with other labels in this repo (single-operator). Lock in `claude` unless conflicts emerge.

## Next step

After this design is approved, invoke the `superpowers:writing-plans` skill to produce a numbered, task-oriented implementation plan covering: schema migration, table modules, MCP tool registrations, Helm values, Discord bot helper, tests, deployment.
