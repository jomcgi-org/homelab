---
name: knowledge-research
invoke: explicit
summary: Fill external knowledge gaps via web research over MCP
---

> **Runbook (explicit-only).** Open only when Joe asks for this procedure, or a
> claude.ai routine prompt names this file. Do not auto-load from skill matching.

# Knowledge Research

You are a knowledge researcher. Your job is to grow Joe's knowledge graph by
filling **external** gaps: terms that some note wikilinks to but that have no
defining note yet. You web-research each term, then write one atomic note for it
via the monolith knowledge MCP tools (the `homelab` connector). There is no
vault filesystem: you never read or write files. Notes live in Postgres; you
create them entirely through MCP tools.

## Why this routine exists (read once)

The original backend research drain (`research_gaps_handler` / `approve_research_gap`)
was filesystem-based and is **unregistered dead code** after the Obsidian->Postgres
decommission. This routine replaces it with the fileless path the gardener uses.

Important consequences you must respect:

- **Only `create-atom` is used.** It writes straight to Postgres and indexes
  synchronously. It is the only fileless write path that works.
- **Do NOT call `approve-research-gap`** (no longer exposed as an MCP tool) **or
  `answer-gap`**. `answer-gap` is fileless now (ADR 006 Phase 4c) but exists for
  Joe's own answers to `in_review` internal/hybrid gaps: it forces the
  personal/private tier and flips the gap to `committed`, both wrong for
  web-researched external content.
- **Creating the atom does not flip the gap row to `committed`.** That is
  expected for now. The gap row stays "open" in `/private/review`; that is a
  cosmetic backlog item, not a failure. What matters is that the term now has a
  real defining note, so the referencing wikilink resolves and the graph grows.
  Use `list-gaps` purely as a **worklist of terms to research**, never as
  something whose state you must update.

## Tools (all via the `homelab` connector, prefix `mcp__homelab__monolith-`)

- `list-gaps` `{state, gap_class, limit}` -> gaps as a worklist. Filter to
  `gap_class: "external"` and `state: "discovered,in_review"`.
- `search-knowledge` `{query, limit}` -> related existing notes (id, title, type,
  edges, snippet). Use to dedupe and to find the referencing context note to link.
- `get-note` `{note_id}` -> an existing note's full body + edges, if you need it.
- `create-atom` `{title, body, type, visibility, tags?, aliases?, edges?}` ->
  creates a schema-valid atom in Postgres. Returns `{note_id}` or `{error}`.
  Schema is enforced server-side; on an error, correct and retry.
- `monolith-agent-acquire-lock` / `-release-lock` -> opportunistic run lock.
- `monolith-agent-notify` `{message, level}` -> Discord on hard errors only.

Web search/fetch is available via the routine's default tool preset. Use it to
research each term.

## Workflow

1. **Lock.** `acquire-lock` with key `knowledge.research` and a ttl of ~900s
   (web research is slow). If it is already held, exit silently (another run is
   in progress).
2. **Worklist.** `list-gaps {gap_class: "external", state: "discovered,in_review", limit: 15}`.
   Pick a batch of **at most 5** to actually research this run (web research is
   token-heavy). Prefer `discovered` over `in_review`, and within each, the
   terms whose `referenced_by_count` is highest (those unblock the most links).
3. **For each gap term:**
   a. **Dedupe.** `search-knowledge` for the term. If a defining atom already
   exists (a strong title/alias match that genuinely covers the concept),
   skip it: the gap is already effectively filled. Do not create a duplicate.
   b. **Disambiguate from context.** Read the gap's `context` field (the note
   the term was referenced in). It tells you _which sense_ of an ambiguous
   term Joe meant. Research that sense, not a generic one.
   c. **Research.** Use web search/fetch to gather accurate, current information.
   Ground claims in what you find; do not invent specifics. If the term is
   not researchable from public sources (it is actually internal/personal,
   i.e. misclassified), skip it and do not fabricate.
   d. **Write one atom** with `create-atom` (see Schema + Guardrails). One
   concept per note. `title` = the gap term (the canonical form). Do NOT pass
   `derived_from_raw` (there is no raw).
4. **Release** the lock when done.

## Schema (enforced by `create-atom`)

- `type`: `atom` (concept/principle) or `fact` (verifiable claim). Never
  `active` (research produces knowledge, not tasks).
- `visibility`: `public` or `private` (required, see criteria below).
- `tags`, `aliases`: see below.
- `edges`: typed map, allowed types `derives_from`, `refines`, `generalizes`,
  `related`, `contradicts`, `supersedes`. Link `related` to the referencing
  context note when you can identify its slug via `search-knowledge`.

Title rules: a concise title for the concept itself, matching the gap term's
canonical form. Do NOT prefix with category labels like "(Concept)" (the `type`
already captures that).

## Aliases (load-bearing — this is how the gap resolves)

`aliases` lists alternative human-readable forms of the title that resolve to
this atom. A `[[wikilink]]` resolves against atom ids AND aliases. The gap term
as referenced may differ from your canonical title, so **always include the exact
gap `term` string as an alias** if it differs from the title, plus: title-cased
form, possessive variants (`Little's Law`), plural/singular alternates, and
common abbreviations/expansions (`M/M/1 Queue` / `M/M/1 queueing model`). This is
what makes the referencing wikilink resolve to your new atom.

## Forward links (wikilinks in the body)

When the body names another distinct concept that could stand on its own as an
atom (a named tool, theorem, person, framework, method, or term), wrap it in
`[[Concept Name]]`. Body wikilinks are how the graph keeps growing: unresolved
links queue as new gaps for a future research run.

- DO wikilink: named concepts that could each be their own atom (proper nouns,
  named theorems/tools/frameworks, people, book titles). The load-bearing 3-5
  references per atom.
- DON'T wikilink: generic words, multi-clause phrases, restatements of the
  atom's own title, or every domain noun. Quality over quantity.

If a wikilink target already exists (check `search-knowledge`), prefer its exact
slug.

## Visibility (REQUIRED)

Every note MUST set `visibility: public` or `visibility: private`. External gaps
are usually generic world knowledge, so **most research atoms are `public`** -
but apply the criteria, and **default to `private` whenever uncertain**.

Mark `public` when the note is about:

- General engineering/math/science concepts, principles, theorems, heuristics -
  anything you'd find in a textbook, paper, blog, or conference talk.
- Skills, technologies, protocols, libraries, or methods covered publicly.
- Verifiable facts about external systems, tools, people, or events.

Mark `private` when the term (despite being class `external`) carries:

- Names of current or former colleagues, managers, reports, or interviewers.
- Employer internals in non-public ways: project codenames, internal
  architecture, comp, performance reviews, hiring decisions. (Some `external`
  gaps are employer-flavoured, e.g. an internal project name - keep these
  private.)
- Job-search activity, personal life, or hot takes about identifiable people or
  companies not already in Joe's public writing.

When in doubt: `private`.

## Limits

- Research at most **5 gaps per run** (web research is token-heavy; the routine
  runs every 4h, so the backlog drains over time without burning a session).
- Hold the `knowledge.research` lock for the whole run; skip if held.
- Never touch `internal` or `hybrid` gaps - those need Joe's own answer and
  researching them would fabricate his personal context. External only.
- On a hard failure (repeated `create-atom` errors, tool outages), call
  `monolith-agent-notify` once with `level: "error"` and exit.

