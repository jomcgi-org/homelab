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
