---
name: qwen-queue
description: Operate and improve the qwen work-queue drainer. Use when queueing qwen-drain jobs, reviewing qwen-agent-for-review PRs, triaging failed drainer jobs, tuning job prompts, or when the user says "queue qwen work", "check the qwen queue", or "/qwen-queue".
---

# qwen queue

The drainer (ADR agents/061, `projects/monolith/swarm/drainer.py`) runs
`claude_agent.routine_jobs` rows of kind `qwen-drain` as one fresh qwen pi
session each, strictly serially, claimed by a `*/15` CronWorkflow tick. The
lane bills nothing, so it exists to convert idle overnight capacity into
audits, reports, and small PRs. This skill is the operating manual plus the
improvement loop.

## Queueing work

Register via MCP `monolith-agent-register-routine-job`:

- `kind: "qwen-drain"`, `payload: {"prompt": ...}` (optional `repo`, `branch`,
  `reasoning`).
- One-shot jobs: `interval_secs` null, `next_run_at` now. On completion
  `next_run_at` goes NULL and the job leaves the queue; `trigger-routine-job`
  re-arms it.
- Recurring jobs: set `interval_secs`. The same name cannot be registered
  twice; to edit a prompt, `deregister-routine-job` then re-register with the
  original schedule.

### Sizing (the watchdog is the real budget)

The per-job ceiling is the EmberVM invoke watchdog at ~920s
(`piRuntimeWorkload.invocation.timeoutSeconds`), not the drainer's 1800s
timeout. A job must finish in **under 10 minutes of qwen time**. Repo-wide
sweeps blow it; per-file and per-directory questions fit. Split "audit all
runbooks" into one job per runbook.

### Prompt shape that works

qwen is a small model. Prompts that one-shot share these properties:

- **One bounded question** with an explicit output contract ("output one line
  per finding as 'file: problem'", "reply with exactly X").
- **Exact numbered steps** for multi-step work (the PR recipe below). Do not
  ask it to plan.
- A **verification clause**: "verify each claim with an actual grep/ls before
  reporting it". Without it, qwen asserts from memory.
- A **size cap** ("keep the whole answer under 1800 characters"): summaries
  land in `last_summary` and Discord.
- For report-only jobs, open with "Report-only task, do not modify files."
- "Never use em dashes" in anything that lands in commits or PRs.

## The PR lane

qwen sessions can open PRs with no extra plumbing: pi guests sit in the egress
lane, the sidecar injects the GitHub token host-keyed (Basic for git push,
Bearer for `api.github.com`), and the guest image ships git, gh, curl, jq.
The working recipe, verified by PR #5333:

1. Find the checkout (`ls /session /session/*` then cd to the dir with
   `.git`).
2. Make the exact edit (spell it out; file, line, old text, new text).
3. `git config user.name "qwen-drainer"` and a noreply email.
4. Branch `qwen/<job-name>`, Conventional Commit.
5. `git remote set-url --push origin https://github.com/jomcgi/homelab.git`
   (the clone origin is the node-local mirror, which cannot receive pushes).
6. `export GH_TOKEN=placeholder` (gh refuses to run without one; the sidecar
   replaces whatever it sends).
7. `gh pr create` with title prefixed `[qwen]`, label `qwen-agent-for-review`,
   the job name in the body. Never enable auto-merge.
8. "Your final answer must be only the PR URL."

If the edit touches a doc that feeds a generated manifest (for example
`knowledge/repo_docs_manifest.ndjson`), the regen-drift check fails PR CI
unless the manifest moves too; qwen has regenerated it correctly when the
prompt allows it, but calling it out explicitly is safer.

### Review policy

Review keys on diff class, not author. Spec'd jobs (the dispatcher wrote the
exact edit): dispatcher checks diff matches spec, PR CI gates, no separate
Opus pass. Discovery jobs (qwen found the defect itself): one Opus review.
A human merges; qwen PRs are never auto-merged.

## Inspecting outcomes (the improvement loop)

1. `monolith-agent-list-routine-jobs` with `kind: "qwen-drain"`:
   `last_status` and `last_summary` are the per-job verdicts.
2. `gh pr list --label qwen-agent-for-review` for the PR backlog (the
   daily-repo-pulse job also reports this).
3. For each failure, classify against the known taxonomy below, then fix the
   PROMPT or the JOB SIZE before blaming the model. Most failures are
   oversized jobs or missing output contracts.
4. Fold any new failure mode into this skill in the same PR that fixes its
   first occurrence.

### Failure taxonomy

| Symptom in `last_summary` | Cause | Fix |
|---|---|---|
| 502 `session invoke failed`, `retryable: false` after ~15 min | invoke watchdog killed an oversized job | split the job; one bounded question |
| literal `<tool_call>` as the whole summary | qwen leaked its tool-call syntax instead of answering | tighten the output contract; demand a specific final-answer format |
| job `error` with a turn-timeout message | session died mid-turn | re-trigger once on a fresh session; if it repeats, the job is oversized |
| jobs due but nothing claimed for hours, ticks firing | a wedged drain_cycle starving the serial DBOS queue (#5328) | check the advisory `drainer` component on private `/api/health` (claim-lag detector, stalled past `agents.drainer.stallThresholdSeconds`); a monolith pod roll clears the wedge via DBOS recovery |
| `next_run_at` NULL, job vanished from due list | one-shot completed; this is normal | `trigger-routine-job` to re-run |

### Harness knobs

`agents.drainer.*` in `projects/monolith/chart/values.yaml`: `enabled` (the
kill switch, flipped in deploy values), `maxJobsPerCycle` (3),
`turnTimeoutSeconds` (1800), `stallThresholdSeconds` (2700, advisory only; a
burst of more than ~9 due jobs can legitimately exceed it), `jobKind`,
`repo`, `branch`. Session-side limits live in `piRuntimeWorkload` in
`projects/embervm/deploy/values.yaml`.

Throughput: ticks every 15 minutes claim up to `maxJobsPerCycle` jobs and run
them serially, so the overnight ceiling is set by wall-clock per job, roughly
4 to 10 small jobs per hour. Queue 40 or more small jobs to cover a 12 hour
window; over-filling is harmless because unclaimed jobs simply wait.

## What qwen is for (and not for)

Good: per-file staleness audits, path-citation checks, TODO inventories,
index-vs-directory drift, single-defect spec'd PR fixes, daily digests.
Bad: anything needing bazel or the test suite (not in the guest), repo-wide
sweeps, judgment calls on prose, multi-file refactors, anything where a wrong
answer is expensive to detect. The dispatcher owns finding defects worth
fixing; qwen owns bounded execution.
