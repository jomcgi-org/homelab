# Knowledge Pipeline

LLM-powered knowledge graph with on-cluster inference.

## Overview

Raw markdown is ingested, decomposed into structured facts by a remote claude.ai gardener routine over MCP (ADR 006 Phase 4c), embedded with voyage-4-nano, and stored in pgvector for semantic search. Fronted by a SvelteKit app with a `Cmd+K` search overlay.

| Module              | Description                                                                                                              |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| **ingest_queue**    | Ingests raw markdown, routes to gardener or direct storage                                                               |
| **gardener**        | Shared decomposition constants/helpers; the decomposition runs as a remote claude.ai routine over MCP (ADR 006 Phase 4c) |
| **gaps**            | Unresolved wikilink lifecycle: discover → classify → review → answer (classifier injected as a callable, fileless)       |
| **store**           | pgvector-backed storage with semantic search                                                                             |
| **service**         | FastAPI service layer                                                                                                    |
| **router**          | HTTP API routes                                                                                                          |
| **mcp**             | MCP tool exposure for AI agent access to the knowledge graph                                                             |
| **links/wikilinks** | Obsidian wikilink parsing and backlink resolution                                                                        |
| **tasks_router**    | Task management API                                                                                                      |

## Search authorization

### Temporal review policy (v1)

Every ordinary write computes a server-side review policy from the persisted
title and claim body. PR, pull request, issue, job, workflow and check claims
with state terms (including head, SHA, open, merged, passing and outstanding)
use `volatile-24h/v1`. Outstanding operational or acceptance gates also use
that policy. `## Evidence`, `## Provenance`, `## Sources` and `## References`
sections are excluded from classification: a durable claim that cites a PR as
evidence stays standard. Everything else uses `standard-90d/v1`. These are elapsed UTC
intervals of 24 and 2160 hours. Caller-supplied deadlines can only shorten them.
Confidence and verification state do not establish freshness.

`observed_at` remains the original evidence observation. Unknown, malformed or
future observations are freshness-unknown and excluded from current context.
At `now >= review_after`, a fact is due. Validity windows, supersession,
invalidation and disputes retain their separate meanings. Expiry resolves no
acceptance gate and deletes no evidence.

Upserts, indexing, extraction retellings and frontmatter round-trips preserve
the original observation and successful review time. They can only keep or
shorten the existing deadline. Extraction duplicate lookup explicitly includes
due facts so a retelling cannot create a fresh copy. Prompt-building lookups
exclude due facts. `freshness.commit_successful_review` is the sole reserved
deadline-renewal function. Slice 2 must add authoritative predicate checks,
durable outcomes and revision/dispute race protection before calling it from
review admission. Unsupported or unavailable checks must leave facts due.

Default search and recall apply expiry in SQL before ranking and again at
hydration. MCP and HTTP `include_history=true` expose due and unknown facts
without widening scope, personal, visibility, legacy or dispute access. Search
responses use `private, no-store`. Query-embedding caches contain no facts.
Persisted recall blocks carry their earliest deadline and are discarded by the
transport before retransmission when due. Already sent agent transcripts are
historical snapshots, explicitly dated and labelled with their expiry.
Volatile facts require a new authoritative observation before action even
inside their 24-hour interval.

**Public tier (decision).** `public_api.knowledge_notes` and
`public_api.knowledge_chunks` do not yet apply review freshness, so a due
published fact can still ground public chat and `public_router` search. This
slice covers MCP, HTTP, recall, explorer and planner context only; the public
views need a migration and a pg-backed test pass, tracked in #6823.

The existing jobs image runs `knowledge-review-backfill --apply --pending-only`
every five minutes through an Argo CronWorkflow with `Forbid` concurrency, a
five-minute deadline and bounded resources. It handles at most 20 batches of
500 notes per run, ordered by stable note id, using only `observed_at` or a
successful review. Rows waiting for backfill have null deadlines and are
freshness-unknown and due. There is no migration-day lease. Dry run is the
default for `knowledge-review-backfill`; its JSON reports policy/freshness
counts and `next_after` for bounded continuation. Replays preserve identity,
provenance, disputes and any shorter deadline. Application commits atomically
and database errors propagate with rollback.

**Why.** Evidence describes what was observed at a particular time. A bounded
review interval limits its use as current context while keeping its historical
value. Read-time enforcement makes that boundary independent of scheduler
availability. Only evidence-backed review may establish a new freshness basis.

The MCP `search_knowledge` tool and `GET /api/knowledge/search` share one
caller-derived policy. Exact `org:`, `repo:`, or `environment:` grants in the
signed principal's `scope` claim are accepted directly. The verified
`homelab-admin` and `kg-agents` groups map to the three exact homelab grants
declared by repository policy, matching their authentik blueprint memberships.
The generic `operators` group, generic OAuth scopes, and server defaults do not
authorize a search. Anonymous callers and authenticated callers without a
mapped grant fail closed.

MCP callers can supply `scope`, for example `repo:jomcgi-org/homelab`, to narrow
search to one exactly matching authorized scope; unauthorized values return the
same empty result as a search with no matches. Narrowed searches exclude legacy
NULL-scoped notes even with `include_personal=true`, and personal opt-in remains
audited before the scope check.

The `kg search` CLI currently supplies only its Cloudflare Access cookie, not
the Authentik bearer required by this policy, so human CLI search is denied.
Issue #6306 tracks acquiring and sending a separate Authentik bearer while
retaining the Cloudflare cookie for edge authentication.

Personal notes are excluded by default. `include_personal=true` is accepted only
when the same principal carries `personal:<subject>` or one of the two mapped
knowledge groups. It includes only `personal:<subject>` and legacy NULL-scoped
notes, and commits one attribution-only audit row before embedding or retrieval.
The audit row contains no raw query or returned content. An audit failure denies
the search. An empty result or a later embedding failure retains the one
committed attempt row. Rows are retained for 90 days. An insert trigger owned by
the migration role prunes older rows in the same transaction, so the agents
tier retains INSERT-only access and an audit commit cannot succeed without its
retention work succeeding too.

Internal recall and extraction callers remain separate: they pass explicit
store filters and never inherit public-entrypoint authorization. Typed graph
edges resolve only when their target is within the same search allow-list.
`POST /api/chat/explore` remains an unscoped retrieval surface and can return
personal and legacy NULL-scoped notes; issue #6307 tracks authorizing it.

`get_note` and `GET /api/knowledge/notes/{note_id}` remain separate direct-ID
authorization surfaces. This change does not make those paths safe merely
because search is filtered, and callers must not treat an ID learned elsewhere
as authorization.

### Acceptance-to-test matrix

| Acceptance | Coverage |
| --- | --- |
| MCP and HTTP default searches use exact org, repo, and environment grants | `mcp_test.py::TestSearchKnowledge`, `router_test.py::TestSearchEndpoint` |
| Personal, cross-subject, cross-repository, and NULL scopes are excluded by default | `store_scoped_test.py::test_search_scope_allow_list_is_applied_before_ranking` |
| Explicit permitted opt-in includes caller personal and NULL scopes with one durable audit | MCP and HTTP opt-in tests plus `store_scoped_test.py` |
| No opt-in creates no audit | MCP and HTTP default-search tests |
| Anonymous, unmapped, and empty allow-lists fail closed | MCP and HTTP authorization tests plus the store empty-list test |
| Authorization is applied before top-N ranking | `store_scoped_test.py::test_search_scope_allow_list_is_applied_before_ranking` |
| Audit failure denies retrieval, while empty results and embedding failures retain one audit | MCP and HTTP failure-path tests |
| Cross-scope edge targets do not resolve | `store_scoped_test.py::test_edge_resolution_uses_search_allow_list` |
| Audit retention and INSERT-only application privileges execute in Postgres | `personal_retrieval_audit_grants_test.py` |
