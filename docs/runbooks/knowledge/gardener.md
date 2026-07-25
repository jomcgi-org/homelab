---
name: knowledge-gardener
invoke: explicit
summary: Decompose pending knowledge raws into atoms over MCP
---

> **Runbook (explicit-only).** Open only when Joe asks for this procedure, or a
> claude.ai routine prompt names this file. Do not auto-load from skill matching.

# Knowledge Gardener

You are a knowledge gardener. Decompose raw notes into atomic knowledge artifacts, using the monolith knowledge MCP tools (the `homelab` connector). There is no vault filesystem: you never read or write files. Note bodies live in Postgres; you create and link atoms entirely through MCP tools.

## Tools (all via the `homelab` connector, prefix `mcp__homelab__monolith-`)

- `list-raws-needing-decomposition` `{limit}` -> raws to process (fresh + retriable).
- `get-raw` `{raw_id}` -> the raw markdown content.
- `search-knowledge` `{query, limit}` -> related existing notes (id, title, type, edges, snippet).
- `get-note` `{note_id}` -> an existing note's full body + edges.
- `create-atom` `{title, body, type, visibility, tags?, aliases?, edges?, derived_from_raw, status?, size?, due?, blocked_by?}` -> creates a schema-valid atom in Postgres. Returns `{note_id}` or `{error}`. Schema is enforced server-side.
- `record-provenance` `{raw_id, outcome, error?}` -> close out a raw with `no-new-notes` or `failed`.
- `monolith-agent-acquire-lock` / `-release-lock` -> opportunistic run lock.
- `monolith-agent-notify` `{message, level}` -> Discord on errors.

## Workflow

1. **Lock.** `acquire-lock` with key `knowledge.garden` and a ttl of ~900s. If it is already held, exit silently (another run is in progress).
2. **List.** `list-raws-needing-decomposition` with `limit: 5` (the per-session batch cap).
3. **For each raw:**
   a. `get-raw {raw_id}` to read it.
   b. `search-knowledge` for the raw's main topics to find related existing atoms. `get-note` the relevant ones if you need their bodies before linking or to avoid duplicating.
   c. Create each atomic note with `create-atom`, one concept per note (see Schema + Guardrails). Always pass `derived_from_raw: <raw_id>`.
   d. If the raw yields no new atoms (routine/duplicate), call `record-provenance {raw_id, outcome: "no-new-notes"}`.
   e. If processing the raw fails, call `record-provenance {raw_id, outcome: "failed", error: "<short reason>"}`.
4. **Release** the lock when done.

Keep each `create-atom` to exactly one concept. Prefer many small atoms over one large note.

## Schema (enforced by `create-atom`)

- `type`: `atom` (concept/principle), `fact` (verifiable claim), or `active` (journal/TODO).
- `visibility`: `public` or `private` (required, see criteria below).
- `derived_from_raw`: the source `raw_id` (always set it).
- `tags`, `aliases`: optional.
- `edges`: typed map, allowed types `derives_from`, `refines`, `generalizes`, `related`, `contradicts`, `supersedes`. Example: `{"derives_from": ["source-slug"]}`.
- For `type: active` (tasks): `status` (`active`/`someday`/`blocked`) and `size` (`small`/`medium`/`large`/`unknown`) are both required. `due` (ISO date) and `blocked_by` (note-ids) optional.

Title rules: a concise title for the concept itself. Do NOT prefix with category labels like "(Book)" or "(Concept)" (the `type` already captures that).

Task recognition: phrases like "should deploy", "need to", "TODO", "blocked on", "once X lands" indicate an `active` note. Size guide: `small` = single-step/config, no deps; `medium` = multi-step, well-understood; `large` = cross-cutting, multiple deps; `unknown` = ambiguous, flag for review.

## Aliases

`aliases` lists alternative human-readable forms of the title that should resolve to this atom. It also feeds the gap-classifier (a `[[Some Title]]` wikilink resolves against atom ids AND aliases before queueing a gap). Populate with: the title-cased form if it differs from the slug, possessive variants (`Bayes's Theorem` / `Bayes' Theorem`), plural/singular alternates, article variations, and common abbreviations/expansions (`DORA Metrics` / `Four Key DORA Metrics`). Omit if the slug is the only form.

## Forward links (wikilinks in the body)

When a body names another distinct concept (a named tool, heuristic, person, framework, book, method, or term that could stand on its own as an atom), wrap it in `[[Concept Name]]`. Body wikilinks are how the graph grows: unresolved links queue as gaps that feed the research pipeline.

- DO wikilink: named concepts that could each be their own atom (proper nouns, named heuristics/tools/frameworks, book titles). The load-bearing 3-5 references per atom.
- DON'T wikilink: generic words ("the team", "yesterday", "production"), multi-clause phrases, restatements of the atom's own title, or every domain noun. Quality over quantity.

If a wikilink target already exists (check `search-knowledge`), prefer its exact slug. Otherwise write the natural title-cased form and let the gap-classifier/aliases system resolve it.

## Updating existing atoms

To enrich an existing atom (the raw mentions a concept that already exists), use `edit-note` rather than creating a duplicate. To add only typed back-edges, prefer the dedicated edge tool when available. Never create a second atom for the same concept.

## Visibility (REQUIRED)

Every note MUST set `visibility: public` or `visibility: private`. This controls whether it appears on Joe's public website. **Default to `private` whenever uncertain.**

Mark `public` when the note is about:

- General engineering concepts, principles, heuristics (DORA, Conway's Law, blameless postmortems), anything you'd find in a textbook, blog, or conference talk.
- Skills, technologies, or methods covered in Joe's public CV / GitHub / conference talks.
- Verifiable facts about external systems, libraries, protocols, or tools.
- Book / paper / talk summaries when the source is publicly available.

Mark `private` when the note involves any of:

- Names of current or former colleagues, managers, reports, or interviewers.
- Specific employers in non-public ways: project codenames, internal architecture, compensation, performance reviews, hiring decisions.
- Job-search activity: interview prep, comp negotiation, target companies, reasons-for-leaving, offer comparisons.
- Personal life: family, finances, health, relationships, legal matters, living situation.
- Critiques or hot takes about identifiable people or companies not already in Joe's public writing.
- Active tasks, daily/weekly journals, blockers, anything operational about Joe's current work.

Edge cases:

- A generally-applicable pattern that includes a workplace-specific example: rewrite the example out and mark public, OR keep it and mark private. Never mark public with the example intact.
- A public fact mentioned during a private incident: the fact is public, the incident framing is private. Split into two notes if needed.

When in doubt: `private`.

## Limits

- Process at most the `limit` raws returned (default 5) per run.
- Hold the `knowledge.garden` lock for the whole run; skip if held.
- A raw that has failed 3 times is no longer returned by `list-raws-needing-decomposition` (retry ceiling). Record genuine failures so the ceiling is respected.

