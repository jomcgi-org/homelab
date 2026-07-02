# Discord Bot

AI-powered Discord bot with embeddings, vision, web search, and channel summarisation.

## Overview

Responds to messages using on-cluster LLM inference, with context from the knowledge graph. Supports history backfill, channel summarisation, multimodal inputs (images via vision), and owner-gated artifact generation via the `/artifact <prompt>` command (goose-backed sessions).

| Module           | Description                                                                      |
| ---------------- | -------------------------------------------------------------------------------- |
| **bot**          | Core Discord bot with message handling and response generation                   |
| **agent**        | LLM agent with tool execution (web search, knowledge graph, vision)              |
| **backfill**     | Historical message import and re-processing                                      |
| **summarizer**   | Channel summarisation with scheduled digests                                     |
| **explorer**     | Conversation exploration and search                                              |
| **vision**       | Image analysis via multimodal LLM                                                |
| **web_search**   | Web search tool integration via SearXNG                                          |
| **changelog**    | Changelog fetching and presentation                                              |
| **goosecracker** | Owner-gated `/artifact` command: stateful artifact generation via goose sessions |
| **outbox**       | Leader-safe Discord outbox so any replica or job can post messages               |
| **store**        | Message persistence with embedding storage                                       |
