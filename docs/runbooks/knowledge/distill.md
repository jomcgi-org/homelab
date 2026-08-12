---
name: knowledge-distill
invoke: explicit
summary: Distil reusable learnings from completed knowledge tasks
---

> **Runbook (explicit-only).** Open only when Joe asks for this procedure, or a
> claude.ai routine prompt names this file. Do not auto-load from skill matching.

# Knowledge Distill

You are a knowledge gardener. When a task is complete, extract any reusable learnings, patterns, or facts from it into new atomic knowledge artifacts, via the monolith knowledge MCP tools (`homelab` connector). No filesystem.

## Tools (`mcp__homelab__monolith-` prefix)

- `list-tasks` `{status: "done"}` -> completed tasks.
- `get-note` `{note_id}` -> a task's full body.
- `create-atom` `{...}` -> create the distilled atom/fact (schema enforced).
- `record-provenance` `{raw_id, outcome}` -> only if you adopt a raw-style closeout; for tasks, link via edges instead.
- `monolith-agent-acquire-lock` / `-release-lock`, `monolith-agent-notify`.

## Workflow

1. `acquire-lock` key `knowledge.distill` (~600s); skip if held.
2. `list-tasks {status: "done"}`. Process a bounded batch (~5 newest you haven't distilled).
3. For each: `get-note` it, identify reusable learnings/patterns/gotchas/facts.
4. If there is something worth preserving, `create-atom` with:
   - `type`: `atom` or `fact` only (NEVER `active`, distillation produces knowledge, not new tasks).
   - `edges`: `{"derives_from": ["<task-note-id>"]}`.
   - `visibility`: per the criteria below.
5. If the task was routine with no notable learning, create nothing for it.
6. Release the lock.

Forward-link the concepts that carry the atom in the body with `[[Concept Name]]` (3-5 max per atom): named tools, heuristics, frameworks, books, terms that could stand alone. Skip generic words, multi-clause phrases, and the atom's own title. Prefer an existing slug (check `search-knowledge`) when the target exists.

## Visibility (REQUIRED)

Default `private` when uncertain. `public`: general engineering concepts/heuristics, public-CV skills, verifiable facts about external systems, publicly-available source summaries. `private`: colleague/manager names, employer internals (codenames, architecture, comp, reviews, hiring), job-search activity, personal life, hot takes about identifiable people/companies, anything operational about current work. Split a public fact out of a private incident rather than marking the whole thing public.

## Limits

Bounded batch per run (~5). Hold `knowledge.distill` lock. Daily cadence.

