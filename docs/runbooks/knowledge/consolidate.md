---
name: knowledge-consolidate
invoke: explicit
summary: Generate daily/weekly task rollup notes in the knowledge graph
---

> **Runbook (explicit-only).** Open only when Joe asks for this procedure, or a
> claude.ai routine prompt names this file. Do not auto-load from skill matching.

# Knowledge Consolidate

You generate task rollup notes so Joe has a single daily and weekly view of due work, via the monolith knowledge MCP tools (`homelab` connector). No filesystem.

## Tools (`mcp__homelab__monolith-` prefix)

- `get-daily-tasks` -> tasks due today or overdue.
- `get-weekly-tasks` -> tasks due this week (Monday-Sunday).
- `list-tasks` `{...}` -> fallback for custom filters.
- `create-atom` / `edit-note` -> write or refresh the rollup notes.
- `monolith-agent-acquire-lock` / `-release-lock`, `monolith-agent-notify`.

## Workflow

1. `acquire-lock` key `knowledge.consolidate` (~300s); skip if held.
2. **Daily rollup:** `get-daily-tasks`. Build a rollup note id `tasks-daily-<YYYY-MM-DD>` listing tasks due today/overdue, sorted by size (large first). If it exists, `edit-note` to refresh; else `create-atom`.
3. **Weekly rollup:** `get-weekly-tasks`. Build `tasks-weekly-<YYYY-Www>` grouped by day (Mon-Sun). Refresh or create the same way.
4. Use `type: fact`, `visibility: private` (rollups are operational), and link each listed task with `[[<task-note-id>]]` so the rollup is navigable.
5. Release the lock.

Rollups are derived views: it is fine to regenerate their body each run. Do not create `active` notes here.

## Limits

Two notes per run (daily + weekly). Hold `knowledge.consolidate` lock. Daily cadence. Rollups are always `private`.

