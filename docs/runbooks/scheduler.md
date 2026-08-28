---
name: scheduler
invoke: explicit
summary: Inspect and trigger Postgres-backed scheduled jobs
---

> **Runbook (explicit-only).** Open only when Joe asks for this procedure, or a
> claude.ai routine prompt names this file. Do not auto-load from skill matching.

# Scheduler

Inspect and trigger the monolith's Postgres-backed job scheduler via the
`homelab` CLI.

## When to Use

- User asks if a scheduled job ran, or when it last ran
- User wants to "kick", "trigger", or "force" a scheduled job to run now
- Investigating calendar-poll / knowledge-ingest / gap-discovery behavior
- Verifying a newly registered job is recognized after a deploy
- Spotting orphan rows (DB row exists but no handler is registered — purged on
  next pod restart)

Scheduler state lives in the database. Use this runbook when the question is
what the scheduler thinks the next or last run is.

NOT for the knowledge gardener: gardening is a claude.ai routine now (see the
knowledge-gardener skill), not a scheduler job. There is no `knowledge.gardener`
row.

## Auth

`homelab` authenticates via Cloudflare Access. First-time auth prompts for a
`CF_Authorization` token; cached on disk afterwards. See the `knowledge` skill
for first-run setup details — same flow.

The `homelab` CLI is a personal tool installed outside this repo. The API it
calls is `projects/monolith/scheduler/router.py` (`GET /api/scheduler/jobs`,
`POST /api/scheduler/jobs/<name>/run-now`). From an MCP-only context,
`monolith-monolith-agent-trigger-job` kicks a job the same way `run-now` does.

## Commands

### List jobs

```bash
homelab scheduler jobs list
```

Output (one job per line):

```
home.calendar_poll        every   900s  next 14:32  never run
knowledge.ingest          every   600s  next 14:18  last ok at 14:08
knowledge.discover-gaps   every  3600s  next 15:08  last error: timeout (last at 14:08)
orphan.removed_handler    every   300s  next 14:20  last ok at 14:15  [orphan]
```

`[orphan]` after a row means the running pod has no handler registered for
that name — the row will be purged on next pod restart by `purge_stale_jobs`.

Add `--json` for raw API output.

### Get a single job

```bash
homelab scheduler jobs get knowledge.ingest
```

Same one-line format as `list`, just for one job. Exits non-zero if the name
is unknown.

### Trigger a job to run now

```bash
homelab scheduler jobs run-now knowledge.ingest
```

Sets `next_run_at = now()` so the next scheduler tick (every ~30s) claims the
job. Idempotent: calling twice in a row is harmless. Concurrency-safe: if a
tick is already running this job, the trigger queues behind the existing run.

Exits non-zero if the name is unknown.

## Workflow

1. **List** to see all jobs and their states
2. **Get** to confirm a single job's interval / next_run_at / last_status
3. **Run-now** to kick a job; then **list** again ~30s later to see
   `last_status` updated

## Tips

- All commands support `--json`
- "Did the ingest run?" → `homelab scheduler jobs get knowledge.ingest`
  and check `last_run_at` + `last_status`
- "It says ok but I don't see effects" means the handler returned without
  raising. The handler may be a no-op when nothing changed.
- Triggering a `run-now` does **not** wait — it returns immediately after the
  DB row is updated. Wait for the scheduler tick (~30s) before re-checking.
