# ADR 063: The Factory Knowledge Graph Learns From Evidence Lanes

**Author:** jomcgi
**Status:** Accepted
**Created:** 2026-09-02
**Builds on:** [061 - The Qwen Work-Queue Drainer](061-qwen-work-queue-drainer.md)
(tick/drive/work-item mechanism, second routine kind); [017 - Domain Event
Schema and Tombstone Semantics](017-domain-event-schema.md) (NATS envelope
for in-cluster events; this ADR's raw lanes are an evidence store, not a
third shape); [054 - The Run View: Pinned Plans, Epistemic Registers, and
Recorded-Not-Inferred Data](054-run-view-pinned-plans-epistemic-registers.md)
(engine-fact/system-belief/agent-testimony split, home for
`verification_state`); [051 - Mid-Turn Session Progress Pushed by the
Guest](051-guest-pushed-mid-turn-progress.md) (guest-has-no-write-path
posture extended here); [060 - Escalation as a Pause, Not a Return, With a
Decision Row](060-escalation-as-a-pause-with-a-decision-row.md)
(notify-plus-decision-row shape `report_distress` reuses); [platform/006 -
Decommission Obsidian, Postgres as the Body of
Record](../platform/006-obsidian-decommission-postgres-interim.md)
(`raw_inputs`/`notes`/`AtomRawProvenance` schema, and the
`knowledge-gardener` routine this ADR's lane replaces)

---

## Problem

The knowledge graph has a schema (`knowledge.raw_inputs`, `knowledge.notes`,
`atom_raw_provenance`, per ADR platform/006) and a single planned feed: an
hourly `knowledge-gardener` claude.ai Routine decomposing raws written by
hand or by capture endpoints. It has no lane for the evidence a homelab
agent platform actually produces continuously: finished Ember sessions
(`agent_sessions.agent_turns`), local Claude and Codex sessions running on
Joe's Mac, an agent's own claim about what it did, a human dispute of a
fact the graph already holds, or a GitHub repository's current state. None
of that reaches `raw_inputs` today, so the graph only knows what someone
manually captured into it.

Two properties of the existing schema make an extraction lane a schema
change, not just a new writer. `knowledge.notes` carries no
provenance-quality signal: every derived fact reads the same whether it
came from a careful human capture or a guest's single unverified inference,
and nothing distinguishes a disputed fact from an undisputed one at query
time. `atom_raw_provenance` versions by `gardener_version`
(`projects/monolith/knowledge/store.py:817`), but a fact's own validity
window and the scope it applies to (a person, a repo, an environment, a
session) are absent, and any future retrieval boundary (#5573, and the
authorization work in #4569 and #4944) needs those as real columns, not
inference over `extra`.

ADR 061 answered "how does a standing backlog become continuous qwen-shaped
progress" for merge-queue-adjacent chores. This ADR answers a parallel
question: what evidence the graph should ingest, in what shape, with what
trust, and who may write to it.

---

## Decision

**Every input becomes an immutable `knowledge.raw_inputs` row before
anything is derived from it.** Body in R2 under its content hash (per ADR
platform/006 Phase 4d, `s3://knowledge/raws/<content_hash>.md`), metadata
and lane-specific detail in the existing `extra` JSONB column, and a new
`source` value per lane: `claude-session`, `codex-session`, `ember-session`,
`agent-report`, `dispute`, `distress`. This is not a new envelope
competing with ADR 017's NATS domain events: ADR 017 governs in-cluster
state-change events consumed by projections and writers; `raw_inputs` is
already the graph's evidence store, and a sixth `source` value extends a
table that exists rather than adding a third shape.

**Derived facts get first-class columns for what today only the human
editing a note in Obsidian could tell you.** `knowledge.notes` gains
`scope`, `verification_state` (`legacy | unverified | verified | disputed |
invalidated`), `confidence`, `valid_from`, `valid_until`, and
`observed_at`. A new `knowledge.disputes` table is keyed by the stable
`note_id` string, not the row's integer FK, because reindexing a note is
delete-then-insert against the same `note_id` and a dispute anchored to a
row FK would silently detach from its fact on the next reindex.
`atom_raw_provenance` keeps versioning by `gardener_version`, unchanged.

Scope vocabulary is `personal:<principal>`, `org:<org>`,
`repo:<owner>/<repo>`, `environment:<cluster>`, `session:<id>`. NULL scope
means legacy vault content predating this ADR, and is treated as personal
by future retrieval scoping (#5573). Scope is a column now and an
authorization boundary once #4569 and #4944 land; this ADR does not claim
it enforces anything yet.

**Extraction is a second routine kind on the ADR 061 drainer, not a new
loop.** A `kg-drain` `routine_jobs` row carries only a `raw_id` in its
payload. The monolith builds the prompt at dispatch time (capped raw body,
related existing notes from embedding search, a source-specific lens for
what that lane's evidence is good for and what to be skeptical of), Luna on
the Codex runtime in an EmberVM guest returns one fenced JSON block of
candidate assertions in its turn result, and the monolith parses that
result and writes atoms and provenance server-side. This reuses ADR 061's
tick, drive, and work-item mechanisms wholesale; `kg-drain` is a
`routine_kind` value, not new infrastructure. A daily job cap on the
`kg-drain` kind bounds OpenAI spend, the same axis the Sol trial (#4913) is
judged on.

Guests never write the graph: no MCP client in this lane, the egress
allowlist excludes the MCP port, and the standing principle that identity
and source are server-stamped (ADR 051's progress pushes, ADR 060's
escalation actors) applies here regardless of whether a guest could
technically reach a write path. The `knowledge-gardener` claude.ai Routine
from ADR platform/006 stays disabled; this lane is the gardener now,
trading a hosted hourly cron for a job-queue consumer that can be paused,
capped, and reasoned about with the same levers as every other drainer job.

| Aspect | Today | Decided |
| ------ | ----- | ------- |
| Extraction trigger | `knowledge-gardener` hourly claude.ai Routine (disabled) | `kg-drain` job on the ADR 061 drainer, per-raw |
| Who writes atoms | n/a, no live extractor | Monolith, server-side, parsing the guest's turn result; guest has no MCP client or MCP-port egress |
| Fact trust / validity / scope | none | `verification_state`, `valid_from`/`valid_until`/`observed_at`, `scope` columns on `knowledge.notes` |
| Dispute anchor | n/a | `knowledge.disputes` keyed by `note_id` (survives reindex) |
| Session evidence | none captured | Ember (leader loop, watermarked), local Claude/Codex (Mac collector) |
| Human-asserted facts | manual note edit only | `report_knowledge` / `dispute_fact` / `report_distress` MCP tools |
| Repo state evidence | none | hourly `kg-drain` diff job against last recorded SHA (#5570) |

### Feeds

**1. Ember sessions.** A leader loop in the monolith renders finished
sessions from `agent_sessions.agent_turns`, rows the control plane already
writes, into `ember-session` raws behind a per-session watermark column. No
guest scraping, no new credential.

**2. Local Claude and Codex sessions.** A launchd cron script on the Mac
(`tools/session_collector/`) uploads sessions quiet for 30 minutes as
capped, locally redacted markdown to a new `POST /api/knowledge/raws`
endpoint, authenticated with the cloudflared user token `tools/cli` already
replays as the `CF_Authorization` cookie. State is a JSON file; an expired
token logs and exits rather than blocking the cron.

**3. Intentional reports**, three MCP tools. `report_knowledge` writes an
unverified raw and queues it. `dispute_fact` opens a `knowledge.disputes`
row immediately, so retrieval surfaces the dispute before extraction ever
runs, and separately queues a `dispute` raw carrying a disconfirming lens;
it never deletes the disputed fact. `report_distress` sends a Discord
notify and retains a `distress` raw that is never extracted, the same
notify-plus-retained-record shape ADR 060 uses for escalation.

**4. GitHub.** An hourly `kg-drain` job diffs the checkout against the SHA
recorded on its last run (#5570), not webhooks.

**5. Environment.** Deployment events only for now (#5571). Honeycomb waits
for an alert pipeline to exist before it becomes a feed (#5572: there is no
alert pipeline today).

---

## Alternatives Considered

**Guest writes atoms over MCP.** Rejected: no MCP path from an EmberVM
guest to the monolith exists today, and building one would let the guest
that produced a candidate fact also assert its own provenance, collapsing
the server-stamped-identity boundary ADR 051 and ADR 060 both hold.

**In-pod qwen extraction, the same shape as the existing `titles.py` job.**
Rejected: Joe's quality bar for graph writes is Luna, not qwen, and a
repo-diff raw benefits from a checkout to verify claims against, which an
EmberVM guest has and an in-pod qwen call does not.

**A Mac daemon with lifecycle hooks, SQLite cursors, and a spool.**
Rejected as more machinery than the feed needs: a cron pass over transcript
files with a quiet-time threshold is idempotent by content hash already, so
a resident daemon buys reliability the cron script already gets for free.

**Tailnet ingress for the collector.** Rejected: the cloudflared user token
path `tools/cli` already uses works headlessly from a cron job, while
Cloudflare Access service tokens do not reach the monolith, because the
private tier's `SecurityPolicy` requires the JWT assertion header
(`projects/inference/deploy/templates/httproute-private.yaml`), which a
service token does not carry. A tailnet path would duplicate an
already-working credential for no gain.

**GitHub App webhooks for the repo feed.** Rejected: nothing here needs
webhook latency, and an hourly diff job keyed on a recorded SHA is
idempotent without the delivery-retry story a webhook receiver would need.

**A new canonical event envelope table.** Rejected: `raw_inputs` plus its
`extra` JSONB is already the immutable evidence store this graph runs on.
ADR 017 already covers the in-cluster event stream; a third envelope shape
here would mean two answers to "what does an event look like" for no
expressive gain over a new `source` value.

**Storing `scope` and `verification_state` in `extra` instead of new
columns.** Rejected: reindexing a note replaces `extra` wholesale, the same
delete-then-insert behavior that forced `disputes` to key on `note_id`
rather than a row FK, and nothing filters on JSONB contents today.
Retrieval scoping (#5573) needs real, indexable columns.

---

## Security

Baseline `docs/security.md`. Guests have no write path to the graph: no MCP
client in the `kg-drain` guest, and the egress allowlist excludes the MCP
port outright, enforced at the network boundary as well as by the
application not offering the tool. All graph writes from extraction are
server-side, parsing a guest's turn-result text the same way ADR 054's
epistemic-register split treats agent output as testimony until something
server-side promotes it. `verification_state` defaults to `unverified`
unless the guest's candidate cites tool-verified evidence, per the lane's
lens; nothing here promotes a fact to `verified` automatically. Personal
scope is not ingested by any lane in this program (the Mac collector skips
non-allowlisted repos), so `scope` carries no authorization weight yet,
only a label, until #4569 and #4944 land. The collector's credential is the
same cloudflared user token `tools/cli` already replays as
`CF_Authorization`; this ADR grants it one new destination
(`POST /api/knowledge/raws`), not a new credential.

---

## Risks

| Risk | Likelihood | Impact | Mitigation |
| ---- | ---------- | ------ | ---------- |
| Raw volume from the Mac collector (roughly 200 MiB/week) grows R2 storage without bound | Medium | Low | retention policy deferred, per-source; raws are cheap object storage, not the hot Postgres path |
| A `kg-drain` job class fails repeatedly and floods OpenAI spend | Low | Medium | daily job cap on `kg-drain`, same lever ADR 061 uses per drainer job class |
| `note_id`-keyed disputes drift from their fact if reindex changes `note_id` derivation | Low | Medium | `note_id` is already the stable identity reindex preserves across delete-then-insert |
| Guest-produced candidate facts get treated as verified by a careless retrieval consumer | Medium | Medium | `verification_state` defaults to `unverified` per ADR 054's register split |
| Cloudflare Access token expiry silently stops the Mac collector | Medium | Low | expired token logs and exits; source transcripts remain on disk, nothing lost |

---

## What Would Make Us Revisit

- **#4569 and #4944 land**, at which point `scope` stops being a label and
  becomes an enforced retrieval boundary, worth re-evaluating against this
  ADR's personal-scope exclusion.
- **Mac collector volume materially exceeds the roughly 200 MiB/week
  estimate**, making the deferred retention policy urgent.
- **A second guest-write path is proposed elsewhere**, the moment to
  revisit "guests never write the graph" deliberately, not piecemeal.
- **Honeycomb gets an alert pipeline (#5572)**, unblocking that feed.

---

## References

| Resource | Relevance |
| -------- | --------- |
| [#5527](https://github.com/jomcgi/homelab/issues/5527) | Parent tracking issue for this ADR's program |
| [#5564](https://github.com/jomcgi/homelab/issues/5564)-[#5568](https://github.com/jomcgi/homelab/issues/5568) | Sub-issues: schema, Luna lane, MCP tools, Ember session raws, Mac collector |
| [#5569](https://github.com/jomcgi/homelab/issues/5569)-[#5574](https://github.com/jomcgi/homelab/issues/5574) | Follow-ups: guest MCP, hourly diff job, deployment events, Honeycomb, scoped retrieval, distress inbox |
| `projects/monolith/knowledge/models.py:238-277` | `RawInput` and `AtomRawProvenance` as they exist today, incl. `gardener_version` |
| `projects/inference/deploy/templates/httproute-private.yaml` | Private-tier `SecurityPolicy`, why Access service tokens don't reach the monolith |
| `#4913` | Sol trial; `kg-drain`'s daily job cap is judged on the same OpenAI-quota axis |

---

## Amendment 2026-09-03

The Tailscale operator already runs in the GKE cluster, so the collector's
default transport is now the monolith tailnet Service, enabled by
`tailnet.enabled`, with no Cloudflare cookie. The cached cloudflared token path
remains the fallback when the tailnet is unavailable. Tailnet membership is
network admission only; per-caller identity remains tracked by #4944.
