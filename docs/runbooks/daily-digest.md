---
name: daily-digest
invoke: explicit
summary: Prioritised outstanding-work digest for Discord
---

> **Runbook (explicit-only).** Open only when Joe asks for this procedure, or a
> claude.ai routine prompt names this file. Do not auto-load from skill matching.

# Daily Digest: what to follow through on

Scans three sources of in-flight work, ranks them by how close they are to
"done but dropped", and posts a single Discord message. The framing is
**follow-through**: surface the things Joe started and has not finished, not
a generic backlog of everything that could ever be done.

Run interactively with `/daily-digest`, or let the
`projects/monolith/claude_routines/daily-digest.yaml` routine fire it on a
schedule.

## Sources

| Source      | Where                                     | Signal of "outstanding"                                              |
| ----------- | ----------------------------------------- | ------------------------------------------------------------------- |
| **Issues**  | `gh issue list` (open)                    | open GitHub issues, the repo's source of truth for outstanding work |
| **PRs**     | `gh pr list` (authored by Joe + review)   | open PRs that need an action from Joe to move forward                |
| **ADRs**    | `docs/decisions/<category>/*.md`           | `**Status:** Draft` (a decision still owed)                          |

This skill is read-only. It never edits ADRs, never pushes, never
touches PRs. Its only side effect is one Discord message.

## Workflow

### 1. Gather open GitHub issues

GitHub issues are the source of truth for outstanding work, so lead with them.

```bash
gh issue list --state open --limit 100 \
  --json number,title,url,labels,updatedAt,parent
```

Group child issues under their parent (the `parent` field / `— ADR <cat>/<NNN> #k` title suffix) so a tracking issue and its sub-issues read as one initiative, not many. Prioritise by label: `critical` and `bug` outrank `enhancement`/`documentation`; `agent-ready` marks work that can be picked up now. If `gh` errors, skip this section (note "issues unavailable" once) and continue.

### 2. Gather pull requests

```bash
# PRs Joe opened that are still open
gh pr list --author "@me" --state open \
  --json number,title,url,isDraft,reviewDecision,mergeable,statusCheckRollup,updatedAt

# PRs waiting on Joe's review
gh pr list --search "review-requested:@me state:open" \
  --json number,title,url,updatedAt
```

If `gh` is not authenticated or returns an error, do not fail the whole
digest: skip the PR section, note "PRs unavailable" once, and continue with
ADRs.

### 3. Gather draft ADRs

Find ADRs whose header line reads `**Status:** Draft`:

```bash
grep -rl "^\*\*Status:\*\* Draft" docs/decisions/
```

A Draft ADR is a decision Joe still owes, distinct from implementation work.

### 4. Rank

Sort into priority tiers. The tier, not a numeric score, is what gets
surfaced, so keep the buckets sharp:

| Tier              | What lands here                                                                                  |
| ----------------- | ----------------------------------------------------------------------------------------------- |
| **P0 - finish**   | Open PR that is approved + mergeable but unmerged, OR failing CI, OR has review comments awaiting a reply. Almost done, one step from shipping. Open issue labeled `critical`. |
| **P1 - in flight**| A parent tracking issue with several open sub-issues (an initiative actively underway); your own draft PRs; open issues labeled `bug` or `agent-ready`. |
| **P2 - decide**   | Draft ADRs; open `enhancement`/`documentation` issues not yet started. |
| **P3 - review**   | PRs requesting Joe's review.                                                                     |

Within a tier, sort by most-recently-updated first. Cap the whole digest at
~12 items; if more remain, append a `+N more` line per truncated tier.

### 6. Post to Discord

Call `mcp__homelab__monolith-monolith-agent-notify` **once** with a markdown
message. Keep it under Discord's 2000-character limit. Shape:

```
**Daily digest, follow-through, <YYYY-MM-DD>**

**P0 finish**
- #412 Cilium default-deny rollout, `critical`, <url>
- #123 Fix collector opt-in, CI red on //projects/platform/otel-collector, <url>
- #119 Auth health check, approved + mergeable, just needs merge, <url>

**P1 in flight**
- #3942 grimoire: live-play platform (tracking), 6 open sub-issues, <url>
- #130 (draft) Per-PR preview envs, <url>

**P2 decide**
- ADR draft: NATS canonical event stream, docs/decisions/agents/016-...md

**P3 review**
- #131 Bump rules_apko, review requested, <url>
```

Set `level`:
- `warn` if anything in P0 has been red/blocked for more than a couple of days,
- otherwise `info`.

If every source comes back empty (no open issues, no open PRs, no draft ADRs),
post a single one-line `info`: "Daily digest: nothing
outstanding, all clear." Do not stay silent, the absence of work is itself
useful signal for a daily cadence.

## Constraints

- Read-only. No edits, commits, pushes, or PR mutations.
- One Discord message per run. Never spam multiple notifies.
- Prefer `gh` and local file reads; this skill needs no cluster access.
- If a data source errors, degrade gracefully and report the rest, do not
  abort the digest.
