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

The MCP `search_knowledge` tool and `GET /api/knowledge/search` share one
caller-derived policy. Exact `org:`, `repo:`, or `environment:` grants in the
signed principal's `scope` claim are accepted directly. The verified
`homelab-admin` and `kg-agents` groups map to the three exact homelab grants
declared by repository policy, matching their authentik blueprint memberships.
The generic `operators` group, generic OAuth scopes, and server defaults do not
authorize a search. Anonymous callers and authenticated callers without a
mapped grant fail closed.

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
| Audit retention and INSERT-only application privileges execute in Postgres | `knowledge/personal_retrieval_audit_grants_test.py` |
