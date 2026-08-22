---
title: Knowledge Graph
date: 2026-08-22
summary: An LLM pipeline that decomposes my notes into structured facts, embeds them, and serves semantic search, both to my agents and to this site's search bar.
public: false
---

An on-cluster model breaks each of my notes into atomic facts and stores them so a question pulls back the right one.

## How it works

**Decomposition.** An on-cluster Qwen model decomposes markdown into structured facts, with a self-critique pass before anything is committed to the graph.

**Embeddings.** voyage embeddings stored in Postgres pgvector with HNSW indexes. One database holds facts, edges, and vectors; no separate vector store to run.

**MCP surface.** Search, notes, tasks, and research-gap tools exposed over MCP, so any Claude session (local or scheduled) can read and write the graph.

**Gap research.** The graph files research gaps for itself. External questions are auto-researched by agents; judgment calls queue for human review.

**This site.** The notes view and Cmd+K search overlay on this site render the same live graph through the same API.

## Source

- [Browse the notes](/app/notes)
- [projects/monolith/knowledge](https://github.com/jomcgi/homelab/tree/main/projects/monolith/knowledge)

<!-- Numbers above were current on 2026-08-22 when this was transcribed from the engineering page. This is a point-in-time post; do not update it, write a new one. -->
