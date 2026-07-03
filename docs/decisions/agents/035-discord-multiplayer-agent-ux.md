# ADR 035: Discord Multiplayer Agent UX (Ambient Classifier, Thread Sessions, Live Task Checklist)

**Author:** Joe McGinley
**Status:** Draft
**Created:** 2026-07-02

---

## Problem

Anthropic's Claude Tag (Slack-only, Enterprise/Team beta) validates a product shape we want on Discord: an agent that behaves like a shared team member in a channel rather than a per-user command runner. Evaluating Tag against the goosecracker/bosun stack, we are ahead on isolation (Firecracker microVMs, ADR 030), cost (self-hosted Qwen), per-server ACLs (ADR 029), and the recipe self-improvement loop, but behind on three interaction-layer capabilities:

1. **No classifier.** The bot only reacts to explicit invocation (`/agent`, mentions). It cannot decide on its own whether a message deserves a response, nor whether the right response is a conversational reply or a full goose agent session.
2. **Opaque mid-turn state.** A running task shows a static "processing" message plus the reaction lifecycle (⏳→👀→✅, #3085). That is a three-state signal; the user cannot see what stage the agent is on, and other channel members get no visibility at all.
3. **Blocking multi-turn.** Follow-up input while a task runs is queued as a whole new task or lost. There is no way to steer a running session, so the interaction feels synchronous and single-player.

Claude Tag's design shows these are cheap to close: its "classifier" is mention-guaranteed attention plus per-channel standing instructions (not a per-message ML relevance model), and its multiplayer story falls out of one primitive: task state lives visibly in the thread, not in a private exchange with the invoker.

---

## Decision

Make the Discord agent surface **thread-native, legible, and steerable**, in five parts.

**1. Conversational acknowledgment.** Every trigger gets an immediate natural-language reply that acknowledges the request and says what the agent is about to do (reusing the concierge-reply path from #3096). This is the "responsiveness" layer: the user hears back in a second, in prose, before any heavy work starts.

**2. Live task checklist.** For work that escalates into a goose session, the ack is followed by a **separate** checklist message that is **edited in place** as stages start and complete. Two messages, not one: the ack stays immutable so Discord notifications show readable prose (notification content is captured at send time), while the checklist message underneath absorbs the churn. Updates land at stage boundaries only, not per token. The plan comes from the session's staged decomposition (the sub-recipe router already produces stages); the reconciler that today flips the done marker (#2928) becomes the writer that patches the checklist message at stage boundaries. The reaction lifecycle stays as the coarse at-a-glance signal; the checklist is the fine-grained one. The whole exchange is visible to the channel, which is what makes the surface multiplayer.

**3. Thread-scoped shared sessions.** A session is keyed by Discord thread, not by invoker. Any message posted into an active session's thread is injected into the running session as steering input, processed in order, instead of being rejected or queued as a separate task. Anyone in the thread can redirect, add constraints, or cancel.

**4. Two-stage classifier (attention, then depth).** Splitting "should bosun respond, and how" into two cheap decisions instead of one smart one:

- **Attention**: a mention of the bot or a reply to it always gets a response. Unprompted messages are evaluated only in channels that have an ambient directive configured; a fast model scores the message against that channel's directive and responds only above a confidence threshold. Whether ambient mode is enabled at all in a channel remains an ADR 029 grant.
- **Depth**: everything enters as conversation. The first conversational turn decides whether a plain reply suffices or the request should escalate into a goose session, extending the existing query/plan/implement/research router (#3034) with a "just chat" branch. Router-as-first-turn, not router-before-turn.

**5. Channel directives as living state, not a git-tracked standard.** The git-tracked prompt defines only the **initial** behaviour: the base persona and the seed directive a channel starts from. The operative per-channel directive lives in Postgres and evolves through a continuous feedback loop: when interactions show what a channel or a user actually wants (style requests like "keep replies short here", "always thread", "stop responding to memes", or repeated corrections), those observations inform updates to the stored directive. Different channels and different users will legitimately want different interaction types and styles; the directive is where that divergence accumulates, without a PR cycle per preference. This is the same seed-in-git, evolve-from-evidence pattern as the /improve-recipes loop (#3037): recipes seed behaviour, production sessions inform recipe updates. Directive updates are written back with provenance (which interaction motivated the change, who asked) so drift is auditable and revertible, and the git seed remains the reset point.

| Aspect                | Today                            | Decided                                                                                          |
| --------------------- | -------------------------------- | ------------------------------------------------------------------------------------------------ |
| Trigger               | Explicit `/agent` / mention only | Mention always; ambient per-channel standing instructions                                        |
| First response        | Static "processing" message      | Natural-language ack explaining what will happen                                                 |
| Progress              | Reactions ⏳→👀→✅               | Reactions + checklist message edited at stage boundaries                                         |
| Session scope         | Invoker-scoped, blocking         | Thread-scoped, shared, steerable mid-run                                                         |
| Chat vs agent routing | Caller picks the command         | First turn classifies and escalates when needed                                                  |
| Behaviour source      | Static prompt in git             | Git seeds initial behaviour; per-channel directive evolves in Postgres from interaction feedback |

---

## Architecture

```mermaid
graph TD
    M[Discord message] --> A{Attention<br/>mention/reply? ambient rule?}
    A -- ignore --> X[No response]
    A -- respond --> ACK[Conversational ack<br/>concierge reply path]
    ACK --> D{Depth router<br/>chat vs goose}
    D -- chat --> R[Plain reply in thread]
    D -- goose --> S[Thread-scoped session<br/>fc-invoke microVM]
    S --> P[Progress reconciler]
    P -- edit at stage boundaries --> C[Checklist message]
    T[Follow-up messages in thread] -- steering, in order --> S
```

Component mapping:

- `projects/monolith/chat/bot.py`: attention gate on `on_message`; today's command handlers become one entry point behind it.
- `projects/monolith/chat/acl.py` + `chat.discord_feature_grant`: new grant kind enabling ambient mode per channel (the on/off privilege stays git/SQL-managed under ADR 029).
- New `chat.channel_directive` table (Postgres): the operative per-channel directive plus per-user style preferences, seeded from the git-tracked base prompt, updated by the feedback loop with provenance columns (motivating message/interaction, requesting user, previous text) so every change is auditable and revertible.
- `projects/monolith/chat/goosecracker.py` / `goosecracker_sessions`: session key becomes the thread id; add an ordered steering queue consumed by the running session.
- `projects/monolith/chat/goosecracker_progress.py`: extend from append-only progress text to structured stage state; the reconciler patches the checklist message via `discord_outbox`.

Discord constraints that shape the design: message edits are rate limited (roughly 5 edits per 5 seconds per channel), so checklist updates are throttled to stage boundaries rather than streamed per token; edits do not trigger notifications, which is desirable for a low-noise progress surface; messages cap at 2000 characters, so long plans truncate to the active window of stages.

---

## Alternatives Considered

- **Per-message ML relevance classifier over all channels.** Rejected: Claude Tag itself does not do this; mention-guaranteed plus opt-in ambient rules gives the same product with near-zero idle cost and no "bot butts in uninvited" failure mode.
- **One message per stage instead of editing a checklist.** Rejected: floods the channel and buries the conversation; edits keep one canonical progress artifact.
- **Single combined ack + checklist message (both edited).** Rejected: Discord notifications capture message content at send time, so editing the ack message makes the notification stale or unreadable; an immutable ack plus a separate edited checklist keeps both surfaces clean.
- **Streaming token output into the thread.** Rejected: Discord edit rate limits make it janky, and stage-level granularity is what humans actually scan for.
- **Keep sessions invoker-scoped and add a handoff command.** Rejected: multiplayer as a bolt-on command never gets used; thread scoping makes sharing the default.
- **Router-before-turn (classify chat vs goose before responding).** Rejected: doubles latency to first response; the ack turn can make the routing decision itself with context in hand.
- **Git-tracked per-channel directives.** Rejected: a PR cycle per style preference kills the feedback loop; channels and users want divergent interaction styles that should accumulate from usage, not code review. Git keeps the seed and the reset point only.

## Security

Baseline per `docs/security.md`. Ambient mode widens the input surface: the bot now reads and may act on messages from anyone in a granted channel, not just command invokers. Mitigations: ambient rules are per-channel grants under the existing ADR 029 ACL (server admins opt in explicitly); escalation to a goose session still runs inside the fc-invoke Firecracker boundary with per-tier guest MCP ACLs (ADR 034); steering messages injected into a running session carry the author's identity so tier and repo-binding checks apply to the most-privileged action requested, not just the session opener. Prompt-injection risk from channel content is unchanged in kind from today's concierge reply but larger in volume; the ambient classifier must never itself hold tool access (classify-only fast model).

Living directives add a second injection surface: channel content can now influence **persistent** behaviour, not just one turn. Mitigations: directive updates require an explicit style/behaviour request attributed to a user whose tier permits it (not passive inference from arbitrary messages); every update stores provenance and the prior text; the git-tracked seed is the always-available reset; directives shape tone, attention, and interaction style only and can never grant tools, widen ACLs, or change the ambient on/off state, which remain ADR 029/034 concerns.

## Risks

| Risk                                                                       | Likelihood | Impact | Mitigation                                                                                                               |
| -------------------------------------------------------------------------- | ---------- | ------ | ------------------------------------------------------------------------------------------------------------------------ |
| Ambient mode responds when unwanted (annoying bot)                         | Medium     | Medium | Confidence threshold, per-channel opt-in, easy grant revocation                                                          |
| Checklist edits hit Discord rate limits during dense stages                | Medium     | Low    | Throttle to stage boundaries; coalesce edits with a min interval                                                         |
| Mid-run steering destabilizes recipes tuned for single prompts             | Medium     | Medium | Steering enters at stage boundaries, not mid-tool-call; /improve-recipes loop watches session outcomes                   |
| Thread-scoped sessions let a low-trust user steer a high-trust session     | Low        | High   | Per-author tier check on each steering message; privileged actions require the acting author's tier                      |
| Fast-model classifier cost/latency on busy channels                        | Low        | Low    | Only channels with ambient grants are evaluated; Qwen inference is self-hosted                                           |
| Directive drift degrades behaviour over time (accumulated bad preferences) | Medium     | Medium | Provenance on every update, reset to git seed, periodic directive-diff review (improve-recipes-style loop)               |
| Directive updates become a persistent injection vector                     | Low        | High   | Updates only from explicit attributed requests gated by tier; directives cannot touch tools, ACLs, or ambient enablement |

## Open Questions

1. How are directive updates applied: immediately on an explicit request, or proposed by the bot and confirmed by the requester (or a channel admin) before persisting?
2. Do per-user style preferences layer on top of the channel directive at reply time, or merge into the channel directive text?
3. Does a steering message that arrives between stages restart planning, or only append constraints to the next stage?
4. Channel-data tools (thread summarization, decisions/open-questions extraction, metrics and charts into artifacts) are the fourth Claude Tag gap; they layer naturally on thread-scoped sessions but are out of scope here. Separate ADR when we get there.

## References

| Resource                                                                        | Relevance                                                                              |
| ------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| [Introducing Claude Tag](https://www.anthropic.com/news/introducing-claude-tag) | Product shape being evaluated                                                          |
| [Claude Tag docs overview](https://www.claude.com/docs/claude-tag/overview)     | Interaction model: mention-guaranteed attention, in-thread checklist, async multi-turn |
| [ADR 024](024-discord-agent-hosted-model-tiers-and-artifacts.md)                | Goosecracker Discord agent foundation                                                  |
| [ADR 029](029-discord-bot-feature-acl.md)                                       | Per-server feature ACL: home of the ambient grant kind                                 |
| [ADR 030](030-fc-invoke-configurable-firecracker-surface.md)                    | Execution isolation for escalated sessions                                             |
| [ADR 034](034-per-tier-guest-mcp-acl.md)                                        | Per-tier guest tool ACLs applied to steering authors                                   |
| PR #2928                                                                        | Reconciler done marker: becomes the checklist writer                                   |
| PR #3034                                                                        | Sub-recipe router: gains the "just chat" branch                                        |
| PR #3085 / #3089                                                                | Reaction lifecycle and queue: retained as coarse signal                                |
| PR #3096                                                                        | Concierge reply: becomes the conversational ack                                        |
