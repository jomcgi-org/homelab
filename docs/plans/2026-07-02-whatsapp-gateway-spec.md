# WhatsApp Channel Gateway: Behavioural Spec

**Decision record:** [ADR 039](../decisions/agents/039-whatsapp-channel-gateway.md) (Accepted)
**Companion plan:** [2026-07-02-whatsapp-gateway.md](2026-07-02-whatsapp-gateway.md)

This spec defines the user-visible behaviour and data contracts for the WhatsApp household agent. The plan sequences the build. Where documents disagree, the ADR wins on rationale and this spec wins on behaviour detail.

Vocabulary: "the group" is the three-participant WhatsApp group (Joe, partner, bot number). "Gateway" is the Go whatsmeow service. "Monolith" is the existing Python API where all agent behaviour lives.

---

## 1. Gateway lifecycle and pairing

**Behaviour.**

- The gateway runs as its own Deployment in the monolith chart (single replica; whatsmeow allows one live socket per device session). Its whatsmeow session persists in the monolith CNPG Postgres under a dedicated `whatsapp` schema (whatsmeow's sqlstore manages its own tables there), so pod restarts resume the session without re-pairing.
- **First boot (no stored session):** the gateway requests a phone-number pairing code for the configured bot number and delivers it to Joe via `monolith-agent-notify` (Discord). Joe enters the code in WhatsApp on the bot number's phone. The gateway confirms pairing success through the same notify path.
- **Logged out / banned:** the gateway enters a `parked` state: it stops consuming the outbox, fires one `monolith-agent-notify` alert (level `error`) naming the cause, and exposes the state on its health endpoint. It does not crash-loop and does not retry registration on its own.
- The gateway holds **no LLM access and no MCP tools**. Its only egress is the WhatsApp servers; its only cluster contacts are Postgres and the monolith inbound endpoint.

**Acceptance.**

- Deleting the gateway pod mid-conversation loses no messages that were already in the outbox, and the new pod resumes the same WhatsApp session without a pairing code.
- Simulating a logout (revoking the linked device from the phone) produces exactly one Discord alert and a `parked` health state within 60 seconds.

## 2. Inbound contract (WhatsApp → monolith)

**Behaviour.** For each message in an allow-listed group, the gateway POSTs to `/internal/whatsapp/inbound` on the monolith with a dedicated bearer token (1Password-managed):

| Field               | Meaning                                                        |
| ------------------- | -------------------------------------------------------------- |
| `group_jid`         | WhatsApp group JID (the channel key)                           |
| `sender_jid`        | Author's WhatsApp JID                                          |
| `sender_name`       | Author's display/push name                                     |
| `message_id`        | WhatsApp message id (for reactions and quoted refs)            |
| `text`              | Message text (v1 is text-only; media arrives as a placeholder) |
| `quoted_message_id` | Set when the message replies to another message                |
| `timestamp`         | Sender timestamp                                               |

- Messages from groups not in `chat.whatsapp_group` (section 6) are dropped at the gateway without forwarding.
- Delivery is at-least-once; the monolith dedupes on (`group_jid`, `message_id`).
- If the monolith is unreachable, the gateway retries with backoff; WhatsApp-side ordering per group is preserved.

**Acceptance.**

- A message sent in the group appears in the monolith inbound log with sender attribution within seconds.
- A message in any other chat (DM to the bot number, unknown group) produces no monolith traffic.

## 3. Outbound contract (`chat.whatsapp_outbox`)

**Data model.** New table mirroring `chat.discord_outbox` (see `projects/monolith/chart/migrations/20260622130000_chat_discord_outbox.sql`) with WhatsApp verbs:

| Column                                              | Notes                                                             |
| --------------------------------------------------- | ----------------------------------------------------------------- |
| `id` bigserial PK                                   | Also the correlation id for edits                                 |
| `group_jid` text not null                           | Target group                                                      |
| `kind` text not null                                | `message` \| `edit` \| `reaction`                                 |
| `content` text                                      | Body for `message`/`edit`                                         |
| `quoted_message_id` text                            | Optional: send as a reply to a specific message                   |
| `edit_of` bigint                                    | For `edit`: outbox id of the original send                        |
| `target_message_id` text                            | For `reaction`: WhatsApp message id to react to                   |
| `reaction` text / `reaction_remove` bool            | Reaction verb, as in the Discord outbox                           |
| `sent_message_id` text                              | Stamped by the gateway after send (enables later edits/reactions) |
| `created_at`, `posted_at`, `attempts`, `last_error` | Same drain semantics as the Discord outbox                        |

**Behaviour.**

- The monolith (any replica) enqueues; the gateway drains oldest-first per group, sends via whatsmeow, stamps `posted_at` and `sent_message_id`. Rows failing repeatedly park with `last_error` (no poison-pill loops).
- All monolith-originated WhatsApp traffic goes through this table. There is no second send path.

**Acceptance.**

- Enqueueing a `message` row results in a group message; the row carries `sent_message_id` afterwards.
- An `edit` row referencing that outbox id updates the same WhatsApp message in place.

## 4. Conversation behaviour (attention, depth, sessions)

**Attention.** Reuses `projects/monolith/chat/attention.py` semantics with WhatsApp triggers:

- A reply to a bot message, or a message containing the bot's trigger name, always engages.
- Other group messages engage only if the group has ambient mode enabled (section 6) and the classifier scores them against the group directive above threshold. The classifier holds no tools.
- v1 default for the household group: **ambient enabled** with a household-shaped seed directive (log things we did, scheduling, questions), per ADR 039 open question 2 resolved as: ambient from day one, revisit if noisy.

**Depth.** The in-monolith depth classify (ADR 035 Phase 4 / ADR 036 seam) decides chat vs agent exactly as on Discord. Chat replies render through the concierge path and land as `whatsapp_outbox` messages. Escalations dispatch through `goosecracker.dispatch.submit()` unchanged.

**Sessions.** WhatsApp has no threads, so:

- Session key is `wa:<group_jid>` in the existing session store (`goosecracker_sessions`), with a `provider` discriminator so Discord thread semantics stay untouched.
- At most one **active** session per group. While it runs, participant messages are steering input at stage boundaries (ADR 035 semantics), attributed per author. A queued further task is acknowledged explicitly in prose ("I'll pick that up after the current one").
- Progress: the ⏳→👀→✅ reaction lifecycle applies to the triggering message; the staged checklist is a bot message edited via `edit` rows. WhatsApp allows edits for ~15 minutes: when the window expires mid-session, the gateway reports the edit failure and the monolith posts a fresh checklist message and continues editing that one.

**Acceptance.**

- "What's the ferry plan?" gets a single conversational reply, no session.
- A task-shaped request gets a prose ack, reactions on the trigger message, and a checklist that updates at stage boundaries; on sessions longer than the edit window the checklist visibly continues in a new message rather than stalling.
- A partner message during a run is treated as steering, not a new task.

## 5. Household capabilities

All capability calls execute in the monolith (or its guests) under the `household` tier; the gateway never calls tools.

**5a. Record into the knowledge graph.**

- A record intent ("record: we hiked Garibaldi", or natural phrasing the depth classify routes to record) produces a bot confirmation of what will be stored, then a raw capture into the knowledge pipeline attributed to the group and author.
- Nothing is captured without the confirmation turn. Ambient conversation is never archived. (Consent boundary, per ADR 039 security.)

**5b. Schedule to Google Calendar.**

- A scheduling intent creates the event and replies with title/time/attendees as created. Ambiguous requests get one clarifying question, not a guess.
- Credential is a cluster-side calendar credential scoped to Joe's calendar (ADR 039 open question 1; the plan carries a fallback of drafting the event into the daily digest for manual confirmation if the credential path slips).

**5c. Q&A.** Conversational questions answer inline via the household directive. Questions over recorded household data route through knowledge search, same as Discord chat.

**5d. Reminders and digests.** Scheduler-driven (CronWorkflow registry, same as existing monolith jobs): a morning digest (today's calendar + open reminders) and ad-hoc reminders created in conversation, both delivered as outbox messages. Digest cadence and quiet hours live in `chat.whatsapp_group` config.

**Acceptance.**

- "record: booked the cabin for August" → bot confirms → a KG raw exists with group provenance; "what did we do last weekend?" retrieves it.
- "add dinner with Sam Friday 7pm" → calendar event exists; the reply names the created slot.
- The morning digest arrives on schedule and respects quiet hours.

## 6. Group registry, tier, and ACL

**Data model.** New table `chat.whatsapp_group`:

| Column                     | Notes                                                  |
| -------------------------- | ------------------------------------------------------ |
| `group_jid` text PK        | Allow-listed group                                     |
| `display_name` text        | Human label                                            |
| `tier` text not null       | `household` for the partner group                      |
| `ambient` bool not null    | Ambient attention on/off                               |
| `directive_seed` text      | Git-seed ref for the group directive (ADR 035 pattern) |
| `digest_config` jsonb      | Cadence, quiet hours                                   |
| `enabled` bool, timestamps | Kill switch without dropping config                    |

- Only allow-listed, enabled groups produce inbound traffic (enforced at the gateway from a config it reads at startup and on change signal).
- The `household` tier maps to a guest/tool subset per ADR 034: knowledge capture + search, calendar, reminders; **no** repo, cluster, or artifact tools. Tier checks are per-author per-message, as with Discord steering.
- Adding any second group is an explicit registry insert (no self-service join behaviour).

**Acceptance.**

- A household-tier session attempting a repo action is refused with a one-line explanation.
- Disabling the group row stops all bot traffic (in and out) without unpairing.

## Out of scope (deferred)

- Media: image understanding and voice-note transcription (natural fit for "record", future amendment).
- Artifact publishing to the group (signed URLs exist Discord-side; revisit with a use case).
- Multi-group / multi-household generalization; living-directive feedback loop beyond the seed (reuse ADR 035 machinery when wanted).
- Any second messaging provider; the generic channel abstraction waits for the evidence this gateway produces.
