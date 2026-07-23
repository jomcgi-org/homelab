# ADR 039: WhatsApp Channel Gateway (whatsmeow) for the Household Agent

**Author:** Joe McGinley
**Status:** Accepted
**Created:** 2026-07-02

---

## Problem

The agent surface (goosecracker/bosun, ADRs 024/025/029/035) is Discord-only. The chat that actually runs the household, the WhatsApp conversation between Joe and his partner, has no agent in it. The capabilities wanted there are the ones already built or in flight for Discord: record what we did into the knowledge graph, schedule events, answer questions, and push reminders and digests.

WhatsApp is hostile terrain for bots in personal chats:

1. **No bot API for personal accounts.** The official Business Cloud API is built for customer service: template messages, a 24-hour reply window, no first-class group participation. It cannot be an ambient member of a family group.
2. **Unofficial clients carry ban risk.** Libraries that speak the multidevice protocol (whatsmeow, Baileys) operate outside WhatsApp's terms; accounts can be banned, so whichever number the bot uses is at risk.
3. **The ecosystem is uneven.** Python options are immature one-off repos; Node options (Baileys wrappers like WAHA/Evolution API) are HTTP gateways around a fast-moving dependency. The mature core is [whatsmeow](https://github.com/tulir/whatsmeow) (Go, `go.mau.fi/whatsmeow`): it powers mautrix-whatsapp and Beeper, tracks the multidevice protocol closely, and persists sessions via a Postgres-backed sqlstore.

The deeper question is architectural: is this "a WhatsApp bot", or a second channel onto the existing agent platform? The Discord stack already separates channel plumbing (bot gateway, `discord_outbox`, reaction lifecycle, ADR 029 ACLs) from channel-agnostic brains (sessions, the query/plan/implement/research recipe router on fc-invoke, knowledge and scheduler surfaces). Everything valuable lives behind that seam.

---

## Decision

Add WhatsApp as a **second channel** onto the existing agent stack via a thin, transport-only gateway. Five parts.

**1. Dedicated throwaway number in a group.** The bot registers its own WhatsApp number (cheap SIM/eSIM) and joins a three-participant group (Joe, partner, bot). Ban risk lands on a disposable number, never Joe's. The gateway's visibility is naturally scoped to chats that number is in; it is never linked to a personal account.

**2. A Go gateway under `projects/monolith`, speaking whatsmeow.** New component `projects/monolith/whatsapp/` built as its own dual-arch apko image and Deployment in the monolith chart (the Python API pod stays untouched). whatsmeow's sqlstore persists the device session in the monolith CNPG Postgres under a dedicated `whatsapp` schema, so the pod is stateless and reschedulable without re-pairing.

**3. Transport-only contract, brains stay in the monolith.** The gateway does exactly two jobs:

- **Inbound:** deliver group messages (sender, group JID, text, quoted-message ref, media refs) to the monolith chat pipeline over an authenticated internal endpoint. Attention, depth routing, session management, recipes, and capability calls all reuse the ADR 035 machinery.
- **Outbound:** consume a `chat.whatsapp_outbox` table (mirroring `discord_outbox`) and translate rows into WhatsApp sends, edits, and reactions.

No LLM calls, no tool access, no business logic in the gateway. When a second non-Discord channel exists, this contract is the evidence for what a general channel abstraction needs; we do not refactor Discord ahead of that.

**4. Session scoping without threads.** WhatsApp groups are a single flat stream (no threads), so the thread-per-session convention from ADR 035 cannot carry over. Instead: at most one active agent session per group; while a session runs, participant messages are steering input (ADR 035 stage-boundary semantics); replies-to-the-bot always engage; other messages pass the ambient attention gate against the group's directive. WhatsApp reactions and message edits exist, so the ⏳→👀→✅ lifecycle and the edited checklist message both carry over.

**5. A `household` trust tier.** The partner is a new kind of principal: fully trusted socially, not an operator of this cluster. The group gets its own tier in the ADR 029/034 sense: conversational replies, knowledge-graph capture (explicit, bot-confirmed, never ambient hoovering of the chat), calendar scheduling, and scheduled digests; no repo access, no cluster tools, no artifact publishing by default. Tier checks apply per author on each message, same as Discord steering.

Operational posture: the gateway treats "logged out / banned" as a first-class state that alerts Joe via `monolith-agent-notify` and parks cleanly; the number is disposable by design and re-pairing is a documented runbook, not an incident.

| Aspect           | Discord today                    | WhatsApp (this ADR)                                   |
| ---------------- | -------------------------------- | ----------------------------------------------------- |
| Transport        | discord.py in the API pod        | Go whatsmeow gateway, own Deployment                  |
| Outbound         | `chat.discord_outbox`            | `chat.whatsapp_outbox`, same consumer pattern         |
| Session scope    | Thread-keyed                     | Group-keyed, one active session, reply-chain steering |
| Trust            | Per-server ACL (ADR 029) + tiers | `household` tier, per-author checks                   |
| Progress         | Reactions + edited checklist     | Same (WhatsApp supports reactions and edits)          |
| Identity at risk | Bot token                        | Throwaway number (ban = alert + re-pair runbook)      |

---

## Alternatives Considered

- **Official WhatsApp Business Cloud API.** Rejected: 24-hour customer-service windows and template messaging cannot express an ambient group member; it solves ban risk by removing the product.
- **Linking Joe's own account as a companion device.** Rejected: a ban would hit Joe's personal number, and a linked session can see every chat he has, making "only the partner group" a code-level promise instead of a structural boundary.
- **Baileys-based HTTP gateways (WAHA, Evolution API).** Rejected: adds a Node runtime and someone else's API surface around the same ToS-grey protocol, with a worse maintenance record than whatsmeow; we would still write an adapter, just against a middleman.
- **Python bindings (neonize) or immature Python libs (e.g. "piwapp").** Rejected: thin, sparsely maintained wrappers over whatsmeow with an FFI layer in the failure path; going straight to the upstream Go library removes a dependency tier. The repo is Go-native anyway.
- **mautrix-whatsapp + Matrix homeserver.** Rejected: battle-tested WhatsApp handling, but it imports a homeserver and a third protocol to bridge one group chat; the operational surface dwarfs the problem.
- **Channel-abstraction refactor first (generalize Discord, then add WhatsApp as adapter #2).** Rejected for now: pays for an abstraction before the first concrete second channel exists to tell us what it needs. The gateway's inbound/outbound contract is deliberately shaped to become that abstraction later.
- **Standalone bot with its own brain (direct LLM calls from the gateway).** Rejected: duplicates the router, tiers, knowledge, and scheduler integration that already exist behind the monolith seam, and creates a second place where agent behaviour must be improved and audited.

## Security

Baseline per `docs/security.md`. New considerations:

- **E2E terminates in the gateway pod.** WhatsApp's end-to-end encryption ends at each participating device; the bot is a participating device. Session keys live in the `whatsapp` Postgres schema; credentials (pairing bootstrap, internal API token) come via the 1Password operator. Nobody else's chats are reachable: the account is the group's third member, not a bridge into anyone's account.
- **The partner is an untrusted-input principal.** Socially trusted, but messages from any group participant are untrusted input to the agent (prompt injection). The `household` tier holds no repo, cluster, or artifact capabilities, so the blast radius of a successful injection is a wrong calendar event or a bad KG note, both auditable and revertible.
- **Knowledge-graph privacy.** Capture is explicit (a record intent, confirmed by the bot) rather than ambient; the partner's messages are not silently archived into Joe's KG. This is a consent boundary, not just a feature choice.
- **Egress.** The gateway needs outbound WebSocket access to WhatsApp's servers; it gets a tightly scoped egress allowance and no ingress route. It authenticates to the monolith with a dedicated token that only reaches the chat-inbound endpoint.

## Risks

| Risk                                                     | Likelihood | Impact | Mitigation                                                                                 |
| -------------------------------------------------------- | ---------- | ------ | ------------------------------------------------------------------------------------------ |
| Number banned by WhatsApp                                | Medium     | Low    | Throwaway number; low-volume personal use; logout/ban alert + re-pair runbook              |
| WhatsApp protocol change breaks whatsmeow                | Medium     | Medium | Track upstream via Renovate; gateway degrades to parked+alert, monolith unaffected         |
| Partner-consent / privacy erosion (chat leaking into KG) | Low        | High   | Explicit-capture-only design; household tier cannot widen itself; partner-visible confirms |
| Session store loss forces re-pairing                     | Low        | Low    | Session in CNPG (backed up); re-pair is a 5-minute runbook                                 |
| Flat group stream makes concurrent tasks confusing       | Medium     | Low    | One active session per group; quoted-reply steering; queued tasks acknowledged explicitly  |
| Gateway becomes a second brain over time (logic creep)   | Medium     | Medium | Transport-only contract stated here; review gate: no LLM/tool calls in the gateway         |

## Open Questions

1. Calendar write path: reuse the account-scoped claude.ai Google Calendar connector via a routine, or provision a cluster-side OAuth/service-account credential for direct writes? (Spec assumes a cluster-side credential; decide at implementation.)
2. Ambient attention default for the group: mention/reply-only at first, with an ADR 035-style directive enabling ambient engagement later, or ambient from day one?
3. Media handling (photos, voice notes): out of scope for v1; voice-note transcription would make "record what we did" much more natural. Future amendment.
4. Second WhatsApp group (or other households) would force multi-group tier mapping; the schema allows it, the product intent is single-group for now.

## References

| Resource                                                                    | Relevance                                              |
| --------------------------------------------------------------------------- | ------------------------------------------------------ |
| [whatsmeow](https://github.com/tulir/whatsmeow) (`go.mau.fi/whatsmeow`)     | The WhatsApp multidevice library the gateway embeds    |
| [whatsmeow sqlstore](https://pkg.go.dev/go.mau.fi/whatsmeow/store/sqlstore) | Postgres-backed session persistence                    |
| [ADR 024](024-discord-agent-hosted-model-tiers-and-artifacts.md)            | Goosecracker foundation; model tiers                   |
| [ADR 029](029-discord-bot-feature-acl.md)                                   | Feature ACL pattern the household tier extends         |
| [ADR 034](034-per-tier-guest-mcp-acl.md)                                    | Per-tier guest tool ACLs applied to household sessions |
| [ADR 035](035-discord-multiplayer-agent-ux.md)                              | Attention/depth classifier, checklist, steering reused |
| WhatsApp gateway spec                                                       | Behaviour and data contracts                           |
| WhatsApp gateway implementation plan                                        | Phased implementation                                  |
