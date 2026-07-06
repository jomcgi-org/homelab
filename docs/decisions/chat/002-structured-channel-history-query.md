# ADR 002: Structured, Scope-Locked Channel-History Query for the Chat Agent

**Author:** jomcgi
**Status:** Accepted
**Created:** 2026-07-06
**Relates to:** [chat/001 - Ambient Feedback Loop and Directive Autopilot](001-improve-ambient-loop.md)

---

## Context

Bosun already has a history-retrieval tool: `search_history` (`chat/agent.py`)
wraps `MessageStore.search_hybrid` (`chat/store.py`), fusing pgvector and
full-text search over `chat.messages`. It answers "what did we say about X" by
returning the top-K relevant messages.

It cannot answer the other shape of question the channels actually ask:
aggregations and exact lookups. When a user asked for a "Spotify Wrapped" summary
(ambient episode 72), Bosun could not count messages per user, find the most
active day, or pull one specific message, so it substituted a vibe summary and
under-delivered; a user then explained the limitation himself ("it just gets
stuff injected before w/e chat prompt so it's not actually able to search").
Top-K-by-relevance is the wrong primitive for "how many messages did X send in
June" or "who posted most" or "show me that message."

The obvious way to add this is to let the model write a query. That is exactly
the thing to avoid. This ADR records how we add real explorability without ever
handing the model a SQL surface.

## Decision

Add one new agent tool backed by one new `MessageStore` method that runs a
**structured, schema-constrained, scope-locked** query. The model chooses the
*filter*; the system chooses the *scope*. Explorability lives entirely in a typed
parameter space, never in query text.

### Shape

The model fills a validated filter, not a query:

- **Filters:** `author` (a username or Discord mention, resolved to `user_id`
  server-side), `since` / `until` (ISO timestamps), `contains` (free text, run
  through the existing `websearch_to_tsquery` FTS path), `message_id` (exact
  Discord id lookup).
- **Aggregations:** an allow-listed `metric` (`count` | `first` | `latest`) and
  an allow-listed `group_by` (`author` | `day` | none).

The server maps each allow-listed enum to a fixed, hard-coded SQL fragment and
column, builds the statement with **bound parameters** for every value, and
executes it. This mirrors the pattern the existing `search_similar` /
`lexical_search` already use: raw `text()` SQL, every value bound, list members
(`exclude_ids`) bound individually rather than interpolated.

### Forbidden list (enforced, not just documented)

1. **The model never emits SQL or a `WHERE` fragment.** Its output is a typed
   filter object; the server owns every character of the statement. There is no
   code path that concatenates model text into SQL, so SQL injection has no
   surface.
2. **`channel_id` is sourced only from `ctx.deps.channel_id`** (populated from
   the Discord `Message` object in `bot.py`, server-authenticated). It is
   **never a field in the tool's parameter schema**, so the model cannot name a
   channel it is not already in. This is the scope lock, and it is why a
   hijacked model still cannot read another channel: the parameter is not the
   model's to set.
3. **`group_by` and `metric` are allow-listed enums mapped to fixed columns.** A
   value outside the allow-list is rejected before any SQL is built; there is no
   interpolation of a model-supplied identifier into the statement.
4. **A row `LIMIT` cap and a per-statement timeout** bound the work, so a broad
   filter cannot become a full-table scan or a `pg_sleep`-style denial of
   service.

### No dedicated read-only DB role in v1

The bot already runs on this exact path as the trusted internal app user, and
the no-SQL-surface design makes writes, DDL, and cross-table reads unreachable
(there is nowhere for model text to enter a query as code). A read-only role
scoped to this one tool would be novel connection plumbing that guards a door
this design has already sealed, and it would not defend the one failure that
matters here (a future scope-pinning regression is a `SELECT` on the same table,
which a read role does not block). So v1 relies on the application-level scope
lock, consistent with the rest of the chat path.

### RLS is the phase-2 scope backstop, not a v1 blocker

If we later want a database-level guarantee on scope, the right tool is
Row-Level Security, not a read role: a policy `channel_id =
current_setting('app.current_channel')` on `chat.messages`, with the resolver
setting that GUC per transaction, makes even an application bug that drops the
channel filter unable to return another channel's rows. This is deferred: it
hardens the failure mode we have already designed out at the application layer,
and the requesting user can already read the channel in question, so it adds no
v1 confidentiality. It is recorded here so the phase-2 path is unambiguous.

## Consequences

- Bosun can answer count/first/latest and per-user / per-day breakdowns and
  exact-message lookups, closing the under-delivery gap that top-K retrieval
  left open, without a new capability surface for the model to abuse.
- The security property is stated as a rule the code enforces ("model picks the
  filter, system picks the scope"), so a future edit that tries to add a
  `channel_id` filter parameter or a free-form query field is a visible
  violation of this ADR, not a silent regression.
- Because the tool reuses the existing `text()` + bound-param convention and the
  existing `ChatDeps` scope injection, it adds no infrastructure: no
  tool-dispatch loop (PydanticAI already provides it), no new role, no new
  connection.
- Explorability grows by extending the allow-listed enum/filter space (a new
  `group_by`, a new metric), each of which is a reviewed, bounded change, rather
  than by widening what the model may express in a query.
