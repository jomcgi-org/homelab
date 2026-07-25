---
name: knowledge-search
invoke: explicit
summary: Search/debug the knowledge graph via homelab CLI
---

> **Runbook (explicit-only).** Open only when Joe asks for this procedure, or a
> claude.ai routine prompt names this file. Do not auto-load from skill matching.

# Knowledge Graph

Query and debug the knowledge graph via the `homelab` CLI.

## When to Use

- User asks what Joe thinks, means, or believes about a topic
- User references a past decision, project, or idea
- Context about Joe's knowledge or opinions would improve your response
- Investigating failed knowledge ingestion or gardener processing errors
- Checking pipeline health after deploying gardener changes
- ANY scenario where Joe's personal notes might be relevant

## Commands

### Search notes

```bash
homelab knowledge search "query" [--limit N] [--type TYPE]
```

Returns compact one-liners with edges:

```
[0.85] dead-letter-queue — Dead Letter Queue Pattern (atom)
  derives_from→book-building-event-driven-microservices, related→exactly-once-delivery
```

### Read a note

```bash
homelab knowledge note <note_id>
```

Prints metadata to stdout, writes full markdown to a tmpfile.
Use `Read` on the tmpfile path to access content on demand.

### Check dead letters

```bash
homelab knowledge dead-letters
```

Lists raws that exhausted all retry attempts:

```
[42] _raw/2026/04/11/note.md (webpage) — invalid JSON [3 retries]
```

### Replay a dead letter

```bash
homelab knowledge replay <raw_id>
```

Removes failed provenance so the gardener retries on its next cycle.

### Tasks

```bash
homelab knowledge tasks
```

List, search, and manage knowledge-graph tasks.

## Workflow

1. **Search** — formulate a natural language query
2. **Judge relevance** — use the compact output (score, title, edges) to decide what's useful
3. **Read selectively** — fetch full content only for relevant notes
4. **Traverse edges** — follow `derives_from` upstream for "why", `refines` downstream for detail

## Repo markdown in the graph (public-chat corpus)

The repo's own markdown (`docs/**`, `projects/**/*.md`, and `CLAUDE.md` files) is
indexed into the knowledge graph so the **public chat** on jomcgi.dev can ground on
project and decision context. It lives in dedicated `knowledge.repo_docs` /
`knowledge.repo_doc_chunks` tables, deliberately separate from the curated `notes`
graph (the gardener and gap loop never touch it), and is surfaced via the
`public_api.knowledge_chunks` view (`note_id = 'repo:' || path`). A private-only
scheduler job (`knowledge.repo_docs_reconcile`, every 6h) reconciles it by content
hash from a committed manifest baked into the image.

**When you edit repo markdown and want it indexed**, regenerate the manifest:

```bash
bazel run //projects/monolith:gen_repo_docs_manifest   # rewrites repo_docs_manifest.ndjson
git add projects/monolith/knowledge/repo_docs_manifest.ndjson && git commit
```

CI enforces this: the Format check fails if the committed manifest is stale (the
error prints the regen command). It is NOT auto-run by `format` (a py_venv_binary
cannot run in the format multirun). The corpus deliberately includes internal docs,
so the public chat can quote them.

## Tips

- All commands support `--json` for raw API output
- Search queries work best as natural language phrases
- After replaying dead letters, re-check after the next gardener cycle
- Edge types: `refines`, `generalizes`, `related`, `contradicts`, `derives_from`, `supersedes`
- If auth fails, the CLI will prompt for `cloudflared access login` automatically

