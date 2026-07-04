# Ambient Assistant Parity: Behaviour Spec

Companion to [ADR 043](../decisions/agents/043-ambient-assistant-parity.md) (rationale) and the [implementation plan](2026-07-04-ambient-assistant-parity.md) (tasks). This document defines behaviour and acceptance criteria.

## Where we stand (parity audit, 2026-07-04)

Everything ADR 035 planned is implemented and live:

| Capability                                        | Status | Where                                                                 |
| ------------------------------------------------- | ------ | ---------------------------------------------------------------------- |
| Mention-guaranteed attention (@-tag, reply)       | Live   | `chat/bot.py::should_respond`, `chat/attention.py`                     |
| Ambient responses in opted-in channels            | Live   | ambient grant (ADR 029) + Qwen classifier, recent-tag threshold        |
| Conversational ack before heavy work              | Live   | concierge reply path (#3096)                                            |
| Live stage checklist, immutable ack               | Live   | stage markers + checklist renderer, one-live-message settle            |
| Thread-scoped shared sessions, mid-run steering   | Live   | steering queue, tier-gated per author (ADR 034)                        |
| Chat vs agent depth routing, in-monolith          | Live   | `attention.needs_agent` (ADR 035 Phase 4 amendment), orchestrator (036) |
| Channel directives + per-user style prefs         | Live   | `chat/directives.py`, propose-then-confirm, `chat/agent.py` tools       |
| History search from chat                          | Live   | `search_history` agent tool                                              |

Missing for "helpful ambient assistant":

| Gap                                               | Feature below |
| ------------------------------------------------- | ------------- |
| Catch-up summarization of a thread/channel        | Feature A     |
| Decisions / action items / open questions extract | Feature A     |
| Reminders and follow-ups                          | Feature B     |
| Charts/artifacts from channel data                | Feature C     |
| Directive evolution from observed corrections     | Feature D     |

## Feature A: Catch-up and decisions extraction (chat route)

**Behaviour.** In any channel or thread where the bot is engaged (mention, reply, or ambient), a user can ask for a catch-up ("catch me up", "summarize this thread", "what happened here since yesterday") or an extraction ("what did we decide", "list the open questions", "action items from this discussion"). The bot answers inline on the chat route: no thread spawn, no guest session, no checklist.

- Retrieval is a bounded, chronological window of the current channel/thread from the `Message` table, newest-first capped by message count and character budget. Discord threads are channels, so the existing `channel_id` scoping covers both.
- Windows over the single-call budget are chunk-summarized (map-reduce) with the existing chat LLM caller.
- The reply states the window it actually covered ("last 240 messages, back to Tue 14:02") so a truncated window is never silently passed off as complete.
- Extraction output is structured prose: decisions, action items (with who, when stated), open questions. Items the model cannot attribute stay unattributed rather than guessed.
- The depth classifier must keep these requests on the chat route (they are about THIS conversation, not the repo).

**Acceptance.**
1. "Catch me up" in a thread with 300+ messages returns a summary within one reply, states its window, and spawns no session thread.
2. "What did we decide about X" over a discussion containing an explicit decision returns that decision attributed to the author.
3. The same asks in a channel where the bot has no grant behave exactly as today (attention gate unchanged).
4. A repo question ("summarize what this repo's chat module does") still escalates to the agent route.

## Feature B: Reminders and follow-ups (time-based)

**Behaviour.** An engaged user can ask the bot to remind them: "remind me in 20 minutes to rejoin", "remind me Thursday 9am to bump the chart", "follow up on this tomorrow". The agent converts the natural-language time using its existing temporal grounding and calls a `set_reminder` tool with an ISO 8601 UTC datetime.

- Stored in a new `chat.reminder` table: channel, author, text, due time, status (`pending`, `delivered`, `cancelled`). One-shot only.
- A monolith scheduler job drains due reminders into `discord_outbox`; delivery posts in the originating channel and mentions the author: `⏰ <@author> reminder: <text>`.
- Users can list their own pending reminders and cancel by asking in chat (tools: `list_my_reminders`, `cancel_reminder`).
- Validation: due time must be in the future and within 366 days; at most 10 pending reminders per user. Violations get a plain-language refusal in the reply.
- Delivery latency tolerance: within one scheduler tick (minutes, not seconds). The bot's confirmation states the resolved absolute time ("ok, Thursday 09:00 PT") so timezone mistakes are visible at set time.

**Acceptance.**
1. "Remind me in 2 minutes to stretch" confirms with the absolute time and posts the mention-reminder in the same channel within one scheduler tick after it is due.
2. "What reminders do I have" lists pending ones with due times; "cancel the stretch one" cancels it and it never fires.
3. An 11th pending reminder is refused with the cap stated.
4. Reminders survive a monolith restart (Postgres-backed, drained by the scheduler, delivered via the durable outbox).

## Feature C: Charts and artifacts from channel data (agent route)

**Behaviour.** "Chart how many messages each of us sent this week" or "make a page visualizing the poll results above" escalates (as today) to the artifact flow, but the guest receives the data instead of fetching it: at dispatch, the monolith extracts a compact structured dataset (JSON: title, columns, rows; hard row cap) from the channel window and ships it as an `/injected-context/` file (ADR 040). The artifact recipe is told the file exists and renders from it.

- Extraction is a structured LLM call over the same bounded window as Feature A, or a direct SQL aggregate for message-activity metrics.
- The guest gains no channel read access (ADR 034 boundary unchanged); a missing or failed extraction degrades to today's behaviour (artifact built from the prompt alone), never a blocked dispatch.
- The rendered artifact cites its source window, same honesty rule as Feature A.

**Acceptance.**
1. A chart request over recent channel content produces an artifact whose numbers match the extracted dataset (spot-checkable in the injected file).
2. Extraction failure still produces an artifact run (fail-open), with the reply noting data could not be extracted.
3. A plain artifact request with no channel-data reference behaves exactly as today (no dataset file injected).

## Feature D: Directive evolution from observed corrections

**Behaviour.** A weekly scheduler job scans ambient-granted channels for recurring style friction directed at the bot (repeated "shorter", "stop replying to memes", "use threads"). When it finds a consistent pattern, it stages a directive proposal via the existing `propose_update` flow and posts the standard propose-then-confirm message. Humans confirm or discard with the existing reactions; nothing changes without confirmation.

- At most one open proposal per channel; the observer skips channels with a pending proposal or one resolved within 14 days.
- The proposal message quotes the motivating evidence (message links) so confirmers can judge it.
- All ADR 035 directive guardrails hold: style-only screen, provenance, git-seed reset.

**Acceptance.**
1. Seeded test transcripts with 3+ corrections of the same kind yield exactly one proposal referencing them; mixed/one-off complaints yield none.
2. A channel with an open proposal is skipped.
3. Confirming applies the directive (existing flow); discarding leaves the directive untouched.

## Out of scope

- Recurring reminders and event-triggered follow-ups ("when this thread gets a reply"): revisit after one-shot usage is proven.
- Persisting extracted decisions (Postgres or knowledge graph): additive follow-up if cross-window recall becomes a real ask (ADR 043 Open Question 2).
- Unprompted per-channel digests: deliberate non-goal (the "bot butts in" failure mode; daily-digest covers Joe's surface).
- Autonomous repo work supply: ADR 038's queue, a different axis entirely.
- The WhatsApp gateway (ADR 039) inherits chat-agent tools when it lands; nothing here is Discord-API-specific except delivery, which rides the outbox.

## Rollout order

A, then B, then C, then D. A is the biggest perceived-helpfulness win at the lowest risk (read-only, chat route). B introduces the first proactive write path. C touches the guest boundary and fc-invoke. D is optional polish and can be dropped if the observer proves noisy.
