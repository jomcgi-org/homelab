# ADR 043: Ambient Assistant Parity (Channel-Data Tools, Reminders, Directive Evolution)

**Author:** jomcgi
**Status:** Accepted
**Created:** 2026-07-04
**Builds on:** [035 - Discord Multiplayer Agent UX](035-discord-multiplayer-agent-ux.md) (attention gate, thread sessions, depth routing, channel directives), [036 - Orchestrator Brief Compiler](036-orchestrator-brief-compiler-tier.md) (host-side routing and brief compilation), [040 - Caller-Provided Context Injection](040-caller-provided-context-injection.md) (the seam that carries channel data into a guest)

---

## Problem

ADR 035 closed three of the four interaction-layer gaps identified against Claude Tag: the attention classifier (mention-guaranteed plus per-channel ambient grants), legible mid-turn state (ack plus edited checklist), and steerable thread-scoped multi-turn sessions. All five phases of its plan are implemented and live, including the in-monolith chat/agent depth split (Phase 4 amendment) and living channel directives with per-user style preferences (Phase 5).

The fourth gap, channel-data tools, was explicitly deferred ("Separate ADR when we get there", ADR 035 Open Question 4). Beyond that enumerated gap, an audit of what "helpful ambient assistant" means in practice shows two more capabilities a channel teammate is expected to have that the bot lacks entirely:

1. **The bot cannot work over the channel's own history as data.** It can search history (`search_history` tool) and it carries a recent context window, but it cannot produce a catch-up summary of a long thread, extract decisions/action items/open questions from a discussion, or turn channel data into a chart. "Catch me up", "what did we decide", and "chart this" are the highest-frequency asks of a channel assistant.
2. **The bot has no notion of the future.** There are no reminders or follow-ups. "Remind me Thursday to bump the chart" or "follow up on this thread tomorrow" fails silently into a chat reply. The monolith already has every ingredient (Postgres scheduler jobs, the durable `discord_outbox`), none of it wired to the chat surface.
3. **Directive evolution only happens on explicit request.** ADR 035 decision 5 described directives evolving from "repeated corrections", but the implemented flow only reacts to explicit style requests via the `propose_directive_update` tool. Nothing observes accumulated friction (users repeatedly telling the bot to be shorter, stop responding to memes) and proposes the directive change those corrections imply.

## Decision

Four decisions, in priority order.

**1. Channel-data read tools run in-monolith on the chat route.** Catch-up summarization and decisions/open-questions extraction are LLM calls over message history the monolith already stores; they get chat-agent tools backed by a bounded window fetch from the `Message` table, chunk-summarized with the existing `build_llm_caller` path when the window exceeds the context budget. No guest, no session, no new data access: retrieval is provenance-scoped to the requesting channel, the same symmetry argument as the concierge reply (everyone in the channel can already read what the tool reads). This follows the ADR 035 Phase 4 amendment's logic: a summarize/extract request is monolith-answerable and must not cold-boot a microVM.

**2. Reminders are the first proactive surface, built from existing primitives.** A `chat.reminder` table, chat-agent tools to set/list/cancel, and a scheduler job that drains due reminders into `discord_outbox` (delivery mentions the requester in the channel where the reminder was set). One-shot only; recurrence is deferred until usage proves demand. Caps (pending reminders per user, max horizon) bound abuse from ambient channels.

**3. Charts and channel-data artifacts go to the guest, with data injected, not fetched.** "Chart X from this thread" already classifies as an artifact escalation. The monolith extracts a compact dataset from the channel window at dispatch time and ships it to the guest as an `/injected-context/` file (ADR 040), so the artifact recipe renders data it was handed. The guest gains no channel read capability, keeping the ADR 034 tier boundary intact.

**4. Directive evolution becomes observed-but-confirmed.** A scheduled observer classifies recent bot-involved exchanges in ambient-granted channels for recurring style friction and, when confident, stages a directive proposal through the existing `propose_update` machinery. The propose-then-confirm gate from ADR 035 is unchanged: the observer can only propose; a human in the channel confirms with the existing reaction flow. Rate-limited to at most one open proposal per channel. Owner decision (2026-07-04): observer noise is acceptable given two conditions, both binding on the implementation: it runs only in channels with an ambient grant (enabled servers opt in via ADR 029), and its sensitivity knobs (minimum evidence count, per-channel cooldown, run cadence) are deploy-time configuration in `values.yaml`, so noise is tuned down with a values edit rather than a code change.

## Alternatives Considered

- **Summarize/extract inside the goose guest.** Rejected for the same reasons as the ADR 035 Phase 4 amendment: 5-10s of microVM overhead for what is one or two LLM calls over data the monolith already holds, plus it would require granting guests channel read access (an ADR 034 boundary widening with no upside).
- **Persist extracted decisions (to Postgres or the knowledge graph).** Rejected for now (YAGNI): on-demand extraction over a bounded window answers the actual asks. If "what did we decide in March" becomes real, persistence is an additive follow-up.
- **A natural-language date parsing dependency for reminders.** Rejected: the chat agent already has temporal grounding in its system prompt; the tool takes an ISO 8601 UTC datetime and the model does the conversion. Validation (future-only, bounded horizon) catches the failure modes that matter.
- **Fully autonomous directive updates from observed friction.** Rejected: ADR 035's security posture (updates require explicit attributed requests; the git seed is the reset point) exists because directives are persistent behaviour. Observation may propose; only a human confirms.
- **A per-channel scheduled digest feature.** Rejected here: the daily-digest routine already covers Joe's own follow-through surface, and an unprompted per-channel digest is the "bot butts in" failure mode ADR 035 deliberately avoided. Revisit only on explicit demand.

## Security

Baseline per `docs/security.md`. No new trust tiers and no guest capability widening. Channel-data tools read only what the requesting channel's members can already read (provenance-scoped queries, as in the concierge reply). Chart datasets cross into the guest as caller-provided injected context (ADR 040), which is already treated as untrusted input by the recipes. Reminders are user-attributed rows that produce outbox posts in the originating channel only; caps bound volume. The directive observer inherits every mitigation from ADR 035 decision 5: proposals only, style-only keyword screen in `propose_update`, provenance columns, human confirmation, git seed reset.

## Risks

| Risk                                                                     | Likelihood | Impact | Mitigation                                                                                    |
| ------------------------------------------------------------------------ | ---------- | ------ | ---------------------------------------------------------------------------------------------- |
| Long-window summarization is slow or truncates badly                     | Medium     | Low    | Hard message/char caps, chunked map-reduce, tool reports the window it actually covered        |
| Reminder delivery drifts or double-fires across restarts                 | Low        | Medium | Status transitions in one transaction; scheduler job is the single drain; outbox is durable    |
| Chart dataset extraction hallucinates numbers                            | Medium     | Medium | Extraction is structured (JSON schema), capped, and the artifact shows the source window       |
| Observer proposes annoying or wrong directive changes                    | Medium     | Low    | Propose-only, one open proposal per channel, human confirm, provenance and reset unchanged     |
| Extra LLM calls load the shared Qwen tier on busy channels               | Low        | Low    | Tools run on demand only; observer is weekly; inference is self-hosted with known headroom     |

## Open Questions

1. Should reminders support "when this thread gets a reply" style event triggers, or stay time-based only? (Time-based only for now.)
2. Do extracted decisions eventually feed the knowledge graph for cross-channel recall, and under what privacy rule?

## References

| Resource                                                              | Relevance                                                    |
| ---------------------------------------------------------------------- | -------------------------------------------------------------- |
| [ADR 035](035-discord-multiplayer-agent-ux.md)                        | Parent decision; Open Question 4 defers channel-data tools    |
| [ADR 036](036-orchestrator-brief-compiler-tier.md)                    | Host-side routing the chart escalation path rides             |
| [ADR 040](040-caller-provided-context-injection.md)                   | Injection seam for chart datasets                             |
| [ADR 034](034-per-tier-guest-mcp-acl.md)                              | Tier boundary the "inject, don't fetch" rule preserves        |
| [2026-07-04 spec](../../plans/2026-07-04-ambient-assistant-parity-spec.md) | Behaviour and acceptance criteria                             |
| [2026-07-04 plan](../../plans/2026-07-04-ambient-assistant-parity.md) | Implementation plan                                           |
