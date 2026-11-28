# Per-server Discord agents

Design for [HOME-905](https://factory-plan.jomcgi.dev/factory/issue/HOME-905).
Each allowlisted Discord server gets its own persistent Hermes agent on ATE.
The agent wakes when the bridge sends it a turn and suspends when idle. It
reads and writes only its own server's Hindsight bank, and it gets only the
MCP tools that its server's values list. The homelab agent's image and
chart exist (slice 2). Slice 4 (HOME-909) routes tagged messages to the
homelab agent: a policy entry `serverAgents` maps each guild to the Agent,
and the bridge keeps one session per channel with request ID
`discord-channel-<channel>` (`operator.go`, `ChannelRequestID`). Until slice 3, the bridge still recalls
and puts memories in each prompt. In a server with `ingest`, each prompt
also carries `channel_messages_since_your_last_turn` (`channel.go`): the
bridge reads one page of the channel's history from Discord before the
tagged message and keeps the messages after Bosun's last one there (at most
50 lines and 8,000 characters, newest kept; 30 lines when no message of
Bosun's is in the page). The boundary comes from Discord, so a retained
ingest buffer or a restarted bridge loses no context. The channel's session
gets each line once. Text that leaves the bridge (prompt, ingest, import)
names mentions (`mentions.go`): `@username (nick)`, `@role`, `#channel`,
`:emoji:`; an unknown user is `@unknown-user`, never a raw ID. See [Joe only](#joe-only-one-session) for
who the bot answers today. Actions: when
the agent's answer holds a fenced `poll` block (JSON, Discord's poll
limits), the bridge posts it as a Discord poll after the text reply
(`poll.go`). Memory stores `[poll: <question>]`, not the JSON. Fenced
`react`, `thread` and `pin` blocks (`actions.go`, at most one of each) react
to the tagged message with 1 to 3 emoji, start a public thread from it that
gets the reply, and pin the reply. The bot's role needs Add Reactions, Create
Public Threads, Send Messages in Threads and Pin Messages; a refused action
is logged and skipped, and the reply goes to the channel when the thread
fails. Memory stores `[thread: <name>]`, `[reacted: <emoji>]` and
`[pinned]` only for actions Discord did. Each `react`, `thread` and `pin`
block takes an optional `target`, and a `reply` block `{"target": ...}`
posts the answer as a reply to that message (`allowed_mentions` parse `[]`,
`replied_user` false). A target is `tagged` (the default), `replied` (the
message the tag replies to) or the `id` of a
`channel_messages_since_your_last_turn` line (`m1` is the oldest). The
bridge maps labels to Discord IDs for this turn only (`Targets`); any other
target is logged and ignored; a pin with such a target pins nothing. A
pin with no target pins the answer. In a server with a `channels` list,
the bridge does not answer a tag inside a new thread: its ID is not listed.
Follow-up: reminders need state that survives a restart, so they wait for a
persisted store. Other controls below are not seen live yet, so each one is
`designed`.

Joe's decisions are on
[HOME-162 comment 12817](https://factory-plan.jomcgi.dev/factory/issue/HOME-162#comment-12817)
(2026-11-26). The goal is "smooth, helpful and fast".

```mermaid
flowchart LR
  D["Discord (untrusted)"] -- "gateway events" --> B["bridge: allowlist, limits, ingest"]
  B -- "retain every message" --> H[("hindsight-discord: bank discord-GUILD")]
  B -- "A2A turn, tagged messages only" --> A
  subgraph S ["atespace discord-GUILD (one per server)"]
    A["Hermes actor (persistent session)"]
  end
  A -- "recall, actor JWT" --> G["Substrate egress gateway"]
  A -- "model call, placeholder key" --> G
  A -- "MCP call, placeholder token" --> G
  G -- "actor JWT; bank fixed by atespace" --> H
  G -- "injects server's OpenRouter key" --> O["OpenRouter"]
  G -- "injects tool token" --> M["allowlisted MCP servers"]
  B -- "reply, parse: []" --> D
```

| Component | Role | Code | Untrusted input | Credentials held |
| --- | --- | --- | --- | --- |
| bridge | The one Discord gateway and policy point | `discord/` | Every Discord message | Bot token, Hindsight tenant key, kagent invocation token |
| Hermes actor | Answers one server's turns with a persistent session | `../harness/hermes/`, `../chart/templates/discord-server-agents.yaml` | Tagged messages, recalled memories, tool output | Placeholders only |
| egress gateway | Injects the model key, tool tokens and actor JWT | `../../platform-factory/substrate/` | Actor requests | Per-atespace injectable Secrets |
| hindsight-discord | Stores and recalls each server's memory | `../../platform-factory/hindsight-discord/` | Retained text | Database login, OpenRouter key |

## Today

Bosun runs one kagent Agent for all four servers. Each tagged message
creates a fresh session actor `session-<id>` and restores it from a
checkpoint. The Bosun worker log shows `Restore timing breakdown` with
`ate.snapshot.fidelity: volumes` (2026-11-24 and 25). The actor runs one turn, checkpoints it and deletes the session
(`agent.go`, HOME-162 "Short sessions"). The bridge recalls before the turn
and retains after it; the agent has no tools except `:online` web search.

Measured on 2026-11-26, one Bosun turn spends:

| Step | Time |
| --- | --- |
| Hindsight recall | 0.5 to 0.6 s |
| gVisor actor create or restore | 0.06 to 0.32 s |
| Model call | 3.4 to 5.4 s |

The model call is about 85% of the turn. No actor snapshot reached the
Garage `ate-snapshots` bucket yet, so session state today has no copy off
the worker volume.

## Hermes fit

Hermes Agent is Nous Research's MIT-licensed agent in Python. Release
v0.23.4 was published on 2026-11-24. Each fact below links its source; the
[last section](#hermes-facts) lists what the docs do not state.

| Need | Hermes | Source |
| --- | --- | --- |
| Take turns from another service | API server (`/v1/runs`, `/v1/responses`), A2A, ACP over stdio, webhooks, one-shot CLI | [api-server](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/api-server.md), [a2a](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/messaging/a2a.md), [integration](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/programmatic-integration.md) |
| Persistent session | `/v1/runs` and `/v1/responses` keep a conversation by `X-Hermes-Session-Key`; Chat Completions uses `X-Hermes-Session-Id`; sessions in SQLite `state.db` | [api-server](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/api-server.md), [memory](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/memory.md) |
| Memory | `MEMORY.md` and `USER.md`, loaded at session start; one external provider, Hindsight among them | [memory](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/memory.md), [providers](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/memory-providers.md) |
| Compaction | Summarises at `compression.threshold` (default 0.50 of the window) with `auxiliary.compression` | [compression](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/context-compression-and-caching.md) |
| MCP client | `mcp_servers`, stdio or Streamable HTTP, `tools.include` per server | [mcp config](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/mcp-config-reference.md) |
| Model | `provider: custom` with `base_url`; OpenRouter via `OPENROUTER_API_KEY` | [providers](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/integrations/providers.md) |
| Tool limits | `agent.disabled_toolsets`, `platform_toolsets`; `approvals.mode` and `unattended_mode` | [configuration](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/configuration.md), [security](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/security.md) |
| Long-lived process | `hermes gateway`; state in `HERMES_HOME`; image `nousresearch/hermes-agent` | [docker](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/docker.md) |

Hermes fits: it runs headless behind an API, keeps sessions on disk and has
a Hindsight provider.

## Joe only, one session per channel

Joe's decisions on 2026-11-27 (HOME-909) change the per-server design
below for now:

- **Who.** The policy's `users: operator` answers only Joe, by the
  `operator-user-id` field of the bot's 1Password item
  (`discord.operator.userIdKey`). Another author's tag gets no turn and no
  reply. Without a readable ID, nobody gets a turn.
- **Where.** `discord.serverAgents.homelab.servers: all` routes Joe's tags
  in all four allowlisted servers to the homelab agent.
- **Sessions.** One Hermes session per Discord channel; a thread has its
  own channel ID, so it has its own session. Each session is a kagent
  Session, so one actor, on the shared Harness: the same key, compaction,
  tools and gate. Each channel has its own turn queue, so a session never
  gets two turns at once. `concurrency: 3` gives the WorkerPool 3
  workers, so 3 channels answer at once; idle sessions suspend. The old
  whole-server session `discord-server-<guild>` is no longer used, and its
  Hermes context does not carry over.
- **Replies.** When Joe's message replies to another message, the prompt
  holds that message's author and text (`reply_to`, at most 2,000
  characters) and `from_you` when Bosun wrote it. A reply to Bosun's
  message starts a turn without the ping. The Joe-only gate still applies.
- **Recall.** Each turn recalls from the bank of the message's own server,
  and the prompt names the server and channel. There is no cross-server
  recall.
- **Snapshots.** Each channel session that suspends writes its own
  snapshot under the Harness `snapshotPolicy` prefix, so storage grows
  with the number of channels Joe uses. Watch the `ate-snapshots` bucket
  size for `discord-1501965852042330302/`.
- **Unchanged.** Ingest and history backfill run for every server as
  before. Bosun's Agent and template stay rendered but get no turns; Joe
  may reuse them as a separate app for friends.

```mermaid
flowchart LR
  M["tag, or reply to Bosun"] --> J{"author = operator-user-id?"}
  J -- "no, or ID missing" --> X["no turn, no reply"]
  J -- "yes" --> R["recall from this server's bank"]
  R --> Q["queue for the channel"]
  Q --> S1["session discord-channel-A"]
  Q --> S2["session discord-channel-B (thread)"]
  S1 & S2 --> P["WorkerPool, 3 workers, shared Harness"]
```

### Session handoff (HOME-944)

A channel gets a new session when the Agent revision or `sessionEpoch`
changes ("New sessions"), or after a blocked turn (#8809). The new session
keeps the channel's working thread
([decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-944#comment-13154)):

```mermaid
sequenceDiagram
  participant H as Hermes (old session)
  participant R as runner
  participant N as notes vault
  participant B as bridge
  participant R2 as runner (new session)
  H->>R: compaction summary (post_llm_call plugin)
  R->>N: handoff_write: <guild>/_hermes/channels/<channel>.md
  B->>B: new epoch or blocked turn: new session
  B->>R2: first turn: SeedNote + last 20 channel messages
  R2->>N: handoff_read
  R2->>R2: handoff note + bridge prompt as the first input
```

- **Note.** The runner's `kagent-handoff` plugin writes the latest Hermes
  compaction summary of the session to `$HERMES_HOME`. After a turn that
  changed it, the runner replaces the channel's note with `handoff_write`
  (at most 16 KiB; no old versions). The notes server takes the server and
  channel from the bridge's session record, never from the caller. Members
  read the note like `MEMORY.md`. A session that never compacted leaves
  the last note as it was. The bridge writes no note when it abandons a
  session.
- **Seed.** On a new session's first turn the runner reads the note with
  `handoff_read` and puts it before the bridge's prompt. The bridge sends
  the channel's last 20 messages, Bosun's own included, within 8,000
  characters, instead of only those since Bosun's last reply, and says so
  in a trusted line before the JSON.
- **Exclusions.** A blocked message, and a reply's quote of it, never
  reach the seed. The session never sees a blocked turn (the runner chains
  past it) or a gloves-off turn (another Agent, and the bridge leaves those
  messages out of the channel lines), so neither can reach a summary. The
  gloves-off Agent has no notes binding, so it writes no note.
- **Tools.** `handoff_read` and `handoff_write` are for the runner, like
  `memory_read` and `memory_write`; the chart refuses a binding that lists
  them for the model.

## 1. Gateway: keep the bridge

**Recommendation.** The bridge stays the only Discord gateway and the only
policy point. Hermes runs with its Discord platform off and receives
turns from the bridge through kagent A2A.

| Concern | Bridge owns gateway (recommended) | Hermes owns gateway |
| --- | --- | --- |
| Bot tokens | One token, one process (no double replies) | One token per agent, or one shared token in four actors |
| Server allowlist (rule 8) | Values, checked before any model call | Hermes config in each actor |
| Rate limits (rule 7) | User, guild and global limits in one place | Not confirmed in Hermes |
| Ingest into Hindsight | Every message, tagged or not | Lost, or each actor must ingest |
| Wake on activity | Bridge sends a turn; actor sleeps between turns | Actor must stay connected to Discord, so it can never suspend |
| No pings (rule 6) | `allowed_mentions {"parse": []}` on every reply | Hermes reply path |
| Untrusted input as data (rule 1) | Bridge wraps Discord text in one JSON value | Hermes sees raw text |

A Discord gateway connection is a long-lived websocket. An actor that holds
one cannot suspend. Hermes' relay mode moves the bot token to a separate
connector, but the actor still holds an open WebSocket to it
([relay](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/messaging/relay.md)).

Hermes does not need its Discord adapter. The adapter calls the API
server's non-streaming `/v1/responses` with `X-Hermes-Session-Key` and
`conversation` both set to the server's session. With `unattended_mode: deny`, a `/v1/runs` or
streaming client still gets approval requests and the run parks in
`waiting_for_approval`; an MCP tool with `trust: untrusted` parks the same
way. So the adapter also answers every approval request with deny, and a
turn never waits on a person. The
Hermes Discord adapter documents user, role and channel allowlists but no
server allowlist ([discord](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/messaging/discord.md)).

## 2. Runtime on ATE

**Recommendation.** Each server has one kagent Agent on a `spec.hermes`
Harness, in its own namespace and WorkerPool. The kagent fork's
`kagent-hermes` runner speaks kagent's private A2A contract and sends each
turn to the Hermes API server on loopback (section 1). It starts
`hermes gateway` on the first turn, with the API on loopback and no
messaging platform. The harness image
(`../harness/hermes/`) holds the runner and the pinned Hermes Agent without
its browser, media, Node.js and compiler parts. The chart renders the
agent from `discord.serverAgents` (`chart/templates/discord-server-agents.yaml`).

The bridge keeps one persistent kagent session per server, the same way the operator session keeps one per thread
(`operator.go`). The session request ID derives from the guild ID, so a
restarted bridge finds the same session. One session serves every channel
in the server; each turn's JSON names its channel and author.

```mermaid
stateDiagram-v2
  [*] --> Running: first turn creates the session actor
  Running --> Suspended: idle, kagent checkpoints the actor
  Suspended --> Running: bridge sends a turn, restore
  Running --> Crashed: actor or worker lost
  Crashed --> Running: RevertSession to last snapshot
  Crashed --> Fresh: revert refused (FAILED_PRECONDITION)
  Fresh --> Running: next generation, Hindsight restores context
```

- **State.** Hermes keeps its home directory (`HERMES_HOME`) on the actor's
  volume: `state.db` (sessions), `memories/` and `config.yaml`. The
  harness writes `config.yaml` from values at start, so git owns config and
  the volume owns only conversation state.
  The checkpoint that kagent writes on suspend carries the volume.
- **Snapshot store.** The Harness `snapshotPolicy` writes to
  `s3://ate-snapshots/<namespace>/`, as Bosun's does. Slice 1 proves that a
  suspend writes an object to Garage before any other server moves.
- **Backup.** Garage keeps three copies on the factory nodes. Substrate's
  PostgreSQL and the snapshots have no off-site backup
  (`projects/factory-cluster/README.md`). Hindsight is the durable record:
  the bridge ingests every message there, so a lost actor costs only
  Hermes' own session notes.
- **Restore.** Order: `RevertSession` to the last snapshot; else a new
  session generation whose first turn carries a server-wide recall. A
  weekly drill deletes the homelab session and checks that the next turn
  answers a question about last week from Hindsight.
- **Suspend wedge.** A suspend over 30 s wedged sessions on kagent v1.0.1
  (kagent#3311/#3312). The controller fork carries kagent#3653, a 2 min
  `snapshotTimeout`. A small Hermes home keeps checkpoints short. An alert
  fires when a suspend takes more than 60 s.
- **Resources.** kagent applies one limit to every runtime (4 CPU, 8Gi).
  Workers request 100m and 512Mi, so a suspended agent holds no memory
  beyond its worker pod. One worker per server; four servers need four
  worker pods.

## 3. Isolation

**Recommendation.** One namespace, atespace and identity per server. The
actor holds placeholders only. The Hindsight bank follows from the actor's
identity at the gateway, never from the prompt.

- **Atespace.** `discord-<guild_id>`, also the namespace. The credential
  provider's `namespacePolicies` grants each atespace only its own Secrets.
- **Model key.** One OpenRouter key per server, each with its own monthly
  limit, labelled `substrate.ate.dev/injectable`. The gateway injects it for
  that atespace only. This keeps HOME-162 rule 2 and bounds each server's
  spend on its own.
- **Hindsight.** Today the tenant key opens every bank, so it never enters
  an actor. Hermes' Hindsight provider runs in `local_external` mode with
  `bank_id: discord-<guild>` and `memory_mode: context`. Its config has no
  key field; the placeholder goes in env `HINDSIGHT_API_KEY`
  ([provider](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/memory-providers.md),
  [keys](https://hindsight.vectorize.io/sdks/integrations/hermes)). The
  gateway replaces the placeholder with an actor JWT (audience
  `hindsight-discord`). A Hindsight extension, like the factory's
  `/ext/connector/retain`, verifies the JWT with the broker's rules
  (`ARCHITECTURE.md`, "Codex relay"): signature, `iss`, `aud`, `exp` and
  `nbf`; subject exactly `actor/discord-<guild>/session-*` with the
  `actor.ate.dev` claim; no `worker-for-actor` token; atespace in an
  `allowedAtespaces` list. It maps the subject to bank `discord-<guild>`
  and refuses every other bank. It refuses retain and reflect for actor
  tokens, so only recall is open. The bank in the config is a convenience;
  the JWT check is the control.
- **JWT binding.** In this repository `actorJwt` exists only on ModelConfig
  (`chart/templates/_binding.tpl`, `discord-operator.yaml`). Which kagent
  resource binds the hindsight-discord origin to an actor JWT is not
  confirmed. The fallback is a per-guild injectable token that the
  extension maps to one bank.
- **Network.** The `hindsight-discord` ingress policy admits the egress
  gateway for that route only. The bridge keeps its own path.
- **Writes.** The bridge stays the only writer of Discord messages
  (section 5).

## 4. Tools

Joe chose the homelab agent's tools on 2026-11-27
([HOME-915](https://factory-plan.jomcgi.dev/factory/issue/HOME-915)): web
search, web fetch, code execution, the Scotland outdoors planner, Hindsight
recall and factory MCP. A headless browser followed on 2026-11-28 (see
"Browser" below). Code execution and the Scotland planner are in
deploy values (`discord.serverAgents.homelab.serverTools`, `mcpServers`).
Web search and fetch are on with the prompt rule only (Joe, 2026-11-28):
each turn holds the server's recalled memories, so a query may carry
private Discord text off Joe's servers. `serverTools.webSearch` and
`webFetch` in deploy values turn them on; the chart then adds
`webToolsPrompt`. Factory access is the private bosun-requests server
(see "Factory requests" below), not factory-mcp.jomcgi.dev (Joe, 2026-11-28,
[HOME-940](https://factory-plan.jomcgi.dev/factory/issue/HOME-940#comment-13098)).
Recall as a tool waits for a bank bound to the turn's server
(see "Recall as a tool" below); the bridge still puts the turn's own
server memories in each prompt. The notes vault tools are built
(see "Notes vault" below); binding them is a deploy values change.

```mermaid
flowchart LR
  H["Hermes actor (placeholders only)"] -- "Messages API + server tools" --> G["Substrate egress gateway"]
  H -- "MCP plan, check, aurora, light" --> G
  G -- "x-api-key" --> A["api.anthropic.com: web_search, web_fetch, code_execution"]
  G -- "no credential" --> S["scotland-mcp.jomcgi.dev"]
```

| Tool | Runs at | Limit | Cost (Joe's Anthropic key) |
| --- | --- | --- | --- |
| `web_search` (`web_search_20250305`) | Anthropic | 5 per Messages request | $0.01 a search, plus result tokens at the model's input price |
| `web_fetch` (`web_fetch_20250910`) | Anthropic | 3 per request; only URLs already in the chat | Result tokens only |
| `code_execution` (`code_execution_20260521`) | Anthropic sandbox, no network | none | Tokens, and container time after the organization's free monthly hours ($0.05 an hour) |
| Scotland `plan`, `check`, `aurora`, `light` | scotland-connector, public, read-only | Hermes MCP timeout | None |
| GitHub `search_repositories`, `search_code`, `get_file_contents`, branches, tags, commits, releases, pull requests, issues (16 read tools) | github-mcp in factory-discord, read-only, public-repositories token | GitHub's API rate limit for the token | None |
| Artifacts `put`, `template`, `list` (no `delete`) | bosun-artifacts in factory-discord, actor JWT | 2 MiB a file; bucket quota (see "Artifacts") | None |
| Notes `notes_list`, `notes_read`, `notes_search`, `notes_write`, `notes_append`, `notes_delete` (to `.trash/`) | bosun-artifacts `/notes/mcp`, actor JWT, server from the bridge | 256 KiB a note (see "Notes vault") | None |

- **Narrow opening.** The kagent fork's Hermes runner (jomcgi-org/kagent#6)
  names only the bound MCP servers in `platform_toolsets.api_server`, so no
  built-in Hermes toolset loads, except `memory` when the Harness sets
  `spec.hermes.memory` (see "Notes vault"), and renders each binding's tools as
  `tools.include`. MCP sampling, elicitation, resources, prompts, Tool
  Search and Nous connectors are off. Hermes builds Anthropic `tools` from
  function tools only, so a runner plugin adds the three server tools to
  each Messages request. The plugin allows one tool call per model step:
  Hermes drops server tool blocks from history, so a server call left
  pending beside an MCP call would never finish. A paused server loop
  (`pause_turn`) ends the turn with the text so far.
- **Egress.** kagent grants the actor only the model origin, the control
  plane and the MCP servers' origins. Anthropic runs the server tools, so
  they add no origin. The gateway's Cilium policy admits
  `scotland-mcp.jomcgi.dev`.
- **New sessions (HOME-944).** kagent pins a session to the Agent
  revision that created it, so an existing channel session keeps its old
  runner, tools and prompt. The bridge derives each channel session's
  epoch from the Agent's `status.latestSuccessfulRevision` and the policy's
  optional `sessionEpoch` override: `hex(sha256(sessionEpoch + "\n" +
  revision))[:12]`, in the request ID `discord-channel-<epoch>-<channel>`.
  The bridge reads the revision at most once a minute. A new revision, or
  a new `sessionEpoch`, moves each channel to a new session on that
  channel's next turn, and the bridge deletes the old session it knew. A
  failed read keeps the last epoch. The bridge never names a revision that
  kagent has not finished, so the Argo CD wave order does not matter. See
  "Session handoff" for what the new session keeps.
- **Scotland sources.** The prompt asks the agent to lead with the verdict
  and to credit each `meta.sources` entry, as the connector's instructions
  require.

### Public GitHub

Joe asked on 2026-11-27 for a public GitHub tool after Bosun answered
"what's new in the substrate repo?" from stale memories. It is GitHub's
own MCP server (`github/github-mcp-server` v2.3.0) in HTTP mode, in
`factory-discord` (`chart/templates/discord-github.yaml`), behind
`discord.github.enabled`.

```mermaid
flowchart LR
  H["Hermes actor (placeholder)"] --> G["egress gateway"]
  G -- "Authorization: discord-github token" --> M["github-mcp /readonly"]
  M -- "same token" --> A["api.github.com"]
```

- **Read only, three times.** The server runs with `--read-only` and
  `--tools` set to the 16 read tools, Hermes registers only those, and the
  URL path `/readonly` sets read-only mode per request. A request header can
  only narrow these settings.
- **Token.** A fine-grained token with "Public repositories (read-only)" and
  no permissions, from 1Password item `factory-discord-github-public`, field
  `token`, which holds `Bearer ` and the token. It only raises the rate
  limit: it cannot read a private repository or write. The OnePasswordItem
  `discord-github` in the agent's namespace is injectable, so the gateway
  sends it as `Authorization`; the actor and the server hold none.
- **Egress.** The server reaches only `api.github.com`; only the egress
  gateway reaches the server.
- **Rule.** The system prompt tells the agent that memories may be stale:
  for code, repositories or releases, use an explore block; for anything
  else current, check with a tool (GitHub when on) and say what it
  checked.

The GitHub tools stay off (Joe, 2026-11-28: no token, "we just clone"):
Bosun answers repository questions with an explore block.

Turning it on, in order:

1. Joe creates the token and the 1Password item.
2. A change sets `discord.github.enabled: true` in deploy values.
3. After Argo CD syncs it, check that kagent shows the homelab Agent's new
   revision as the latest successful one and that the actor's compiled
   tools include `mcp__github__*`.
4. Existing channels move to that revision on their next turn ("New
   sessions"), with no `sessionEpoch` change.

Any prompt change, such as the check-current-facts rule, reaches existing
channels the same way.

### Browser

Joe chose a self-hosted, signed-out browser under gVisor on 2026-11-28
([HOME-931](https://factory-plan.jomcgi.dev/factory/issue/HOME-931#comment-13034)),
not a hosted browser. Web fetch reads only static pages; the browser reads
pages that JavaScript builds. It is Microsoft's Playwright MCP server
(`mcr.microsoft.com/playwright/mcp` v0.0.86, by digest) in `factory-discord`
(`chart/templates/discord-browser.yaml`), behind `discord.browser.enabled`.
Each routed server agent gets it as MCP server `playwright`, because
`browser` is a Hermes toolset name.

```mermaid
flowchart LR
  H["Hermes actor"] --> G["egress gateway + egress-scan (HOME-932)"]
  G --> B["browser-mcp :8080 (gVisor, Chromium)"]
  B -- "80, 443 only" --> W["public internet"]
```

- **Tools.** Hermes registers only `discord.browser.tools`: navigate, back,
  snapshot, find, click, hover, type, fill form, select, press key, handle
  dialog, wait, screenshot, tabs and close. The chart refuses
  `browser_evaluate` and `browser_run_code_unsafe` (they run JavaScript) and
  `browser_file_upload` and `browser_drop` (they read local files).
- **Signed out.** `--isolated` keeps each profile in memory, and each MCP
  session gets a new browser context. There is no stored profile, cookie,
  storage state or secret, and the pod's disks are emptyDirs. Pages cannot
  add tools (`--no-webmcp`) or keep service workers.
- **Sandbox.** The image runs Chromium with `--no-sandbox`; gVisor
  (`runtimeClassName: gvisor`) is the sandbox between page code and the node.
- **Network.** Only Substrate's egress gateway connects in. Out: the public
  internet on 80 and 443. The Cilium policy denies `discord.browser.deniedCIDRs`
  (private, node, link-local and special ranges), and a CIDR rule never
  selects cluster pods.
- **DLP.** The browser holds no credential, so its page traffic is not
  scanned. The gateway scans the agent's calls to it with egress-scan, which
  denies a call that carries a credential (HOME-932).
- **Rule.** The prompt tells the agent to use web fetch first, to build a URL
  only from Joe's message or search results, never to type memories, channel
  text or secrets into a page, and never to sign in or create an account.
- **Limits.** One replica, 1.5 GiB memory. A page that needs sign-in, a
  CAPTCHA or a file upload fails. Turning it on for existing channels follows
  the same order as the GitHub tools: deploy values first, then a new
  `sessionEpoch` after kagent shows the new Agent revision.

### Tool security

Each turn's prompt holds other people's channel text, recalled memories,
web pages and tool results. All of it is untrusted, and none of it may
cause a write.

- **Writes.** The bound write tools are artifacts `put`, which changes
  only the Joe-only artifact bucket, and, when bound, the notes vault's
  `notes_write`, `notes_append` and `notes_delete`. The prompt allows a
  notes write only when Joe's tagging message asks to note, save or
  delete something, and keeps other people's messages and memories out
  of a note unless Joe asks. The vault's server comes from the bridge's
  session record, not from the model; nothing deletes a note for good.
  With memory sync, Hermes' `memory` tool edits `MEMORY.md` and
  `USER.md`, which every member can read and every channel's prompt
  holds. The prompt (`discord.notes.memoryPrompt`) allows a memory edit
  only when Joe's tagging message asks to remember or forget something, or
  Joe's own words in it state a lasting fact about him, never from other
  people's messages, recalled memories, pages, notes or tool results. The
  prompt is the control here too; Hermes' own scan refuses injection-like
  entries. `delete` is not bound: it cannot be
  undone. The prompt allows `put` only when the tagging message asks for a
  page or file, asks for a new path unless Joe asks to replace one, and
  keeps memories and other people's messages out of a file unless Joe
  asks. The artifacts server checks only the actor JWT, so the prompt is
  the control; a bridge-issued, per-turn grant for the operation and path
  is the follow-up. Each turn carries the gateway's `server_id` and
  `channel_id`, which `put` and `list` take.
  The only other write is bosun-requests' `create_request`, under the same
  rule, and only on a turn by Joe.
- **Discord actions.** The bridge turns a fenced poll, react, thread or
  pin block in an answer into a Discord action. A fetched page could hold
  such a block, so the prompt allows one only when the tagging message
  asks for that action and never from a page, memory or tool result. The
  bridge does not check this yet; a follow-up can require the action word
  in Joe's message before it acts.
- **Allowlists, not prompts, stop new tools.** `tools.include` lists each
  MCP tool by exact name; the compiler refuses a binding without a list, a
  `requireApproval` binding (Hermes denies every approval) and header
  values with `${`, which Hermes expands.
- **Placeholders only.** The actor holds `kagent-credential-injected` for
  the model key; the egress gateway replaces it.
- **Exfiltration.** A search query and a fetched URL leave the cluster
  through Anthropic. The prompt forbids memories, private details or
  secrets in a query or URL; web fetch reads only URLs already in the chat.
  A planted URL in channel text can still be fetched. Each turn holds the
  server's recalled memories in the same session that searches, so the
  prompt is the only control on what a query carries: memory text could
  reach a search engine. Joe accepted this on 2026-11-28.
- **Untrusted results.** The prompt tells the agent never to follow
  instructions in tool results, pages or memories.

### Factory requests (HOME-940)

Joe tags Bosun with a request; the factory builds it; Bosun replies to the
same message with the ticket and PR links when the ticket is Done or
Canceled. bosun-requests (`requests/`, `templates/discord-requests.yaml`)
is in-cluster only: Cilium admits the egress gateway on `POST /mcp` and the
bridge on the reply routes.

```mermaid
sequenceDiagram
  participant H as Hermes
  participant R as bosun-requests
  participant B as bridge
  participant C as connector
  H->>R: create_request (actor JWT)
  R->>B: GET /sessions/<id>: guild, channel, message, author
  R->>R: author is Joe? INSERT bosun_request
  C->>C: file HOME issue (Todo, task-agent), follow it
  B->>R: GET /replies (finished, not answered)
  B->>B: reply to the origin message
  B->>R: POST /replies/<id>/sent
```

| Control | Where |
| --- | --- |
| The server, channel, message and author come from the bridge's record of the turn, not from tool arguments | `requests/mcp.go`, `sessions.go` |
| Only a turn by Joe's Discord user ID files a request; one request per message | `requests/mcp.go`, `bosun_request.message_id` unique |
| The login `factory_bosun_requests` may insert a request, read `bosun_request_status` and set `replied_at` on a finished request only | connector migration `20261128140000_bosun_requests.sql` |
| The connector files with a keyed create, so a replay files no second issue | `connector/dispatch/bosun_requests.go` |
| A reply goes only to an allowlisted server and a channel in that server, with no mentions | `replies.go` |

`request_status` shows only the current server's requests. After HOME-78,
Done means verified live, so the reply then means live.

### Recall as a tool

hindsight-discord has a recall-only MCP token for the four server banks
(`resources/bindings.py`, live since #8759), but no Agent binds it yet.
The token accepts any of the four banks and the model names the bank, and
nothing trusted tells Hindsight which server a turn came from. A fetched
page or a memory could then steer a recall into another server's bank,
and the answer would post that server's memories in a group channel.
Options, recommended first:

| Option | Enforcement | Cost |
| --- | --- | --- |
| Bind the bank to the session: the bridge records channel to server for each session it creates, and the Hindsight extension accepts an actor JWT and allows only that session's server bank | Gateway and Hindsight, no prompt | Hindsight reads the bridge's session record (`GET /sessions/<id>`, built for the notes vault) and takes an actor JWT on the RemoteMCPServer |
| One Agent per server, each with a one-bank token (section 3's design) | Credentials per atespace | Four Agents and WorkerPools; Joe's cross-server session goes |
| Bind the token now and keep the prompt rule (own bank unless Joe names another server) | Prompt only | Injected text can still cause a cross-server read |

### Remember (HOME-919)

When Joe tags Bosun and asks it to remember a fact, the bridge stores
that fact in the server's bank. The model gets no retain tool and no
write credential: only the bridge writes, and only for Joe
([Joe's decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-919#comment-12933)).

```mermaid
flowchart LR
    M[Tagged message] --> O{From Joe?<br/>fromOperator}
    O -- no --> T[Normal turn]
    O -- yes --> P{"remember X" /<br/>"remember that X"}
    P -- no --> T
    P -- yes --> R[RetainFact: own document,<br/>tags channel + kind:remembered]
    R -- stored --> N1[Trusted note: saved]
    R -- failed --> N2[Trusted note: not saved]
    N1 --> T
    N2 --> T
```

- `RememberFact` (`remember.go`) reads the text after the bot mention:
  `remember X`, `remember that X` or `remember: X`, with an optional
  `please`, in any case. A question is not a fact.
- The stored line is `[kind=remembered] Joe asked Bosun to remember
  (channel <id>, <time>): <fact>`. It is its own document, keyed by the
  tagging message, so a server with `ingest` keeps both the message and
  the fact.
- The bridge puts a trusted note before the prompt, outside the Discord
  JSON, and the model confirms briefly. A failed store is logged, the
  note says the fact was not saved, and the turn still answers.
- The exchange retain does not change.

### Notes vault (HOME-918)

Joe decided on 2026-11-28 that Bosun keeps notes per Discord server in
Garage, as an Obsidian-style vault
([decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-918#comment-12931)),
and that Hermes' own memory files live in the same vault
([decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-921#comment-12939),
which supersedes "MEMORY.md stays off"). One session per channel means each
channel actor has its own volume, so files on the volume would split per
channel and live only in snapshots.

```mermaid
sequenceDiagram
  participant B as Bridge
  participant H as Hermes actor (session S)
  participant G as Egress gateway
  participant N as bosun-artifacts /notes/mcp
  participant S3 as Garage bosun-notes
  B->>B: record S -> server, channel
  B->>H: turn
  H->>G: notes_write (no server argument)
  G->>N: actor JWT names session S
  N->>B: GET /sessions/S
  B-->>N: server_id, channel_id
  N->>S3: PUT <server>/<path>.md (notes key)
```

| Path under `<server-id>/` | What | Who writes |
| --- | --- | --- |
| `<path>.md` | Notes; `[[Note name]]` links | `notes_write`, `notes_append` |
| `_hermes/MEMORY.md`, `_hermes/USER.md` | Hermes' memory files, read by every member (Joe chose this; `USER.md` is about Joe). `SOUL.md` stays in git | The runner's sync (`memory_write`, HOME-921) |
| `.trash/<path>.md` | Deleted notes; a taken name gets a UTC time suffix | `notes_delete` (a move) |
| `.history/<path>/<time>.md` | The version a replace overwrote | `notes_write` with `replace`, `memory_write` |

- **Server from the turn.** The actor JWT names the session
  (`session-<id>`). The bridge records each server agent session's server
  and channel before it sends the turn (`sessions.go`), and serves the
  record at `GET /sessions/<id>`; only bosun-artifacts reaches it, GET
  only (Cilium). No tool takes a server argument, and an unknown argument
  is refused. A session the bridge has not recorded, or a server outside
  `bridge.policy.guilds`, gets no vault. The record is in memory: after a
  bridge restart a session is known again at its next turn, the only time
  it calls a tool.
- **No hard delete.** `notes_delete` copies the note to `.trash/` and then
  removes the original, so a failure leaves two copies, never none.
  `.trash/` and `.history/` stay out of `notes_list` and `notes_search`
  unless `include_trash` is set; `notes_read` reads them, so Joe can get
  a note back. The model cannot write `_hermes/`, `.trash/` or `.history/`.
- **Writes.** The prompt (`discord.notes.prompt`, added when a binding is
  named `notes`) allows a write, append or delete only when Joe's tagging
  message asks for it, and never copies other people's messages or
  recalled memories into a note unless Joe asks. Reading is allowed any
  time.
- **Memory sync (HOME-921).** The Harness's `spec.hermes.memory` names the
  `notes` binding (chart `discord.serverAgents.<key>.memory`, on when that
  binding exists). The fork's runner (jomcgi-org/kagent#8) then turns on
  Hermes' memory tool and syncs `MEMORY.md` and `USER.md` with
  `memory_read` and `memory_write`, which return a file and its version
  (SHA-256, `""` for a missing file). The model never gets these two
  tools: the chart and the compiler refuse a binding that lists them.

  ```mermaid
  sequenceDiagram
    participant R as Runner (channel A actor)
    participant N as /notes/mcp
    participant O as Channel B actor
    R->>N: memory_read (before the turn)
    N-->>R: content, version v1
    R->>R: memories/ for Hermes; the turn edits MEMORY.md
    O->>N: memory_write from v1 (B wrote first)
    N-->>O: version v2
    R->>N: memory_write from v1 (after the turn)
    N-->>R: conflict, B's copy at v2
    R->>R: apply A's entry adds and removes to B's copy
    R->>N: memory_write from v2
    N-->>R: version v3
  ```

  - **Load.** Before each turn the runner writes the vault's copy into
    `$HERMES_HOME/memories/`. A new session puts both files in its prompt.
    A continuing session reuses the prompt it started with, for the prompt
    cache, so another channel's change reaches it only when Hermes
    rebuilds the prompt (at compaction) or the channel moves to a new
    session. Its memory tool still edits the newest copy.
  - **Write-back and merge.** After a turn a changed file goes back from
    the version the runner read. On a conflict the runner applies this
    session's entry changes (Hermes entries split on `\n§\n`) to the newer
    copy and retries, at most 3 times. A merge over Hermes' limit (2,200
    characters for `MEMORY.md`, 1,375 for `USER.md`) keeps the newer copy's
    entries, applies only this session's removals, and logs. A missing
    local file loads again; it never empties the vault's copy.
  - **Outage.** A failed call logs and the turn goes on with the local
    copy; the next turn writes it back. A missing file starts empty. The
    runner's last synced copy is in `$HERMES_HOME`, so a new process on the
    same volume still merges.
  - **No background saves.** `nudge_interval: 0` turns off Hermes'
    periodic memory review, which would save facts from a conversation
    without the model's turn choosing to.
  - **Credential.** The runner calls the binding's URL with the binding's
    placeholder header. kagent binds the actor JWT per origin and header,
    and the actor's TCP goes to the egress gateway, so the gateway injects
    the token for the runner's calls too.
- **One replica.** bosun-artifacts runs one pod, and a lock for each key
  makes append, delete and the version check atomic. During a rollout an
  old and a new pod serve for a moment, so two writes to one key can race
  then; `.history/` keeps the version a write replaced.
- **Key.** Secret `bosun-notes-s3` (garage-init `GRANTS`) is only in the
  bosun-artifacts pod, as `NOTES_*` variables beside the artifact key. A new
  pod waits for garage-init to fill it; the old pod keeps serving.

**Wiring.** A deploy values entry binds it to the homelab agent
(`discord.serverAgents.homelab.mcpServers`): name `notes`, URL
`http://bosun-artifacts.factory-discord.svc.cluster.local:8080/notes/mcp`,
`actorJwt.audiences: [bosun-artifacts]`, tools `notes_list`, `notes_read`,
`notes_search`, `notes_write`, `notes_append` and `notes_delete`. With
that entry the chart also renders `spec.hermes.memory` and the memory
prompt (`discord.notes.memoryPrompt`); the controller must be the fork
build with jomcgi-org/kagent#8, else it ignores the field and memory stays
off. Existing channels move onto it on their next turn (see "New
sessions").

#### Notes viewer (HOME-920)

Members read their server's vault at `https://notes.jomcgi.dev/<server-id>/`,
read-only, like an Obsidian vault
([decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-920#comment-12935)).
Only Bosun writes notes. The viewer is `bosun-notes-view`
(`notesview/`, chart `discord-notes-view.yaml`), in the connector image.

```mermaid
sequenceDiagram
  participant F as Member's browser
  participant E as Edge (Envoy)
  participant A as Authentik
  participant V as bosun-notes-view
  participant S3 as Garage bosun-notes
  F->>E: GET /<server-id>/n/Plans.md
  E->>A: no session: sign in (factory-edge)
  A-->>E: ID token, groups claim
  E->>E: rule server-<id>: group discord-<id> or Admins
  E->>V: GET, no Cookie or Authorization
  V->>S3: list and get <server-id>/ (read-only key)
  V-->>E: page, CSP self only
  E-->>F: page, CSP, nosniff, no Set-Cookie
```

- **Pages.** A file tree; the note as HTML; "Linked from" (backlinks);
  search over names and text (`/search?q=`); a graph of links
  (`/graph`). A root note `index`, `README` or `Home` is the front page.
  `_hermes/` shows (Joe's choice). `.trash/`, `.history/` and every other
  name that starts with `.` never show, never resolve a link and answer
  404.
- **Wikilinks.** `[[Name]]`, `[[Name|label]]` and `[[Name#heading]]`
  resolve like Obsidian: by file name, case-insensitive, `.md` optional;
  a target with `/` is a path from the vault root. With two notes of one
  name, the shorter path wins. A link to no note shows as plain text.
- **Server.** Only the URL's first path segment names the server. It must
  be a `bridge.policy.guilds` key (`NOTES_SERVERS`); every other path
  answers 404. The pod reads only that `<server-id>/` prefix.
- **Index.** The pod lists and reads a server's notes at most once per
  30 s (`discord.notes.viewer.indexTTL`) and keeps them in memory: at most
  2,000 notes and 32 MiB per server. Listing skips the hidden keys.
- **Untrusted text.** goldmark renders Markdown with raw HTML off. A link
  or image URL must be relative, `http`, `https` or `mailto`; any other is
  removed. The graph script builds its elements with `textContent` from a
  JSON block. `notesview_test.go` holds an XSS corpus.

| Control | Where |
| --- | --- |
| One route rule and SecurityPolicy per server folder (`artifacts.servers`); each allows only `discord-<server-id>` and `authentik Admins`; default deny | edge `notes-route.yaml` |
| CSP `default-src 'none'`, scripts, styles and images from `'self'`, `connect-src 'none'`, `frame-ancestors 'none'`; `nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`; `Set-Cookie` removed; `Cookie` and `Authorization` not sent to the pod. The app sets the same | edge `notes-route.yaml`, `values.yaml`; `notesview/server.go` |
| Read-only key `bosun-notes-viewer-s3` on `bosun-notes`, in this pod only | `garage_init.py` `GRANTS` |
| In: only the edge proxy. Out: only Garage S3 (3900). Non-root, read-only root, no service account token | chart `discord-notes-view.yaml` |
| DNS and tunnel: the hostname in the tunnel route; only `edge` may route it | `edge-tunnel/resources/route.yaml`, `edge-admission` |

Members get access as for artifacts ("Who can open a file" and
"Membership changes" below).

### Artifacts (HOME-909)

The homelab agent can publish a page or file and give Joe a link. The
`bosun-artifacts` MCP server (`artifacts/`, chart
`discord-artifacts.yaml`) writes to Garage bucket `bosun-artifacts`.
Garage's website endpoint serves it at `https://artifact.jomcgi.dev`.

**Who can open a file (HOME-917).** A Discord sign-in at
`factory-auth.jomcgi.dev` puts a person in Authentik group
`discord-<server-id>` for each allowlisted server they are in. The edge
then admits them to that server's folder:

| Path | Readers |
| --- | --- |
| `/<server-id>/<channel-id>/…` | Group `discord-<server-id>`, and `authentik Admins` |
| `/private/<channel-id>/…` | `authentik Admins` only |
| `/_lib/…` (pinned public libraries, GET and HEAD) | Anyone, no sign-in |
| `/s/<key>?X-Amz-…` (a share link, GET and HEAD) | Anyone with a valid, unexpired link Joe confirmed |
| Every other path | `authentik Admins` only |

A member sees no other host: the gateway-wide policy admits only
`authentik Admins` and `factory-operators`. Bosun files a page under the
current message's server ID. It uses `private` when Joe asks for a private
file, or when the file holds a direct message or text from a channel that
some members cannot read. The folder is per server, not per channel: every
member of the server can open every file in it.

```mermaid
sequenceDiagram
  participant F as Friend's browser
  participant E as Edge (Envoy)
  participant A as Authentik
  participant D as Discord
  F->>E: GET /<server-id>/<channel>/page.html
  E->>A: no session: sign in (factory-edge)
  A->>D: Discord OAuth, scopes identify email guilds
  D-->>A: profile, then /users/@me/guilds
  A->>A: set discord-<id> groups to the allowlisted servers
  A-->>E: ID token, groups claim (10 min)
  E->>E: rule server-<id>: group discord-<id> or Admins
  E-->>F: page from Garage, CSP sandbox
```

**Membership changes.** Groups sync at each Discord sign-in, never in
between. The Discord sign-in session lasts 24 hours
(`factory-social.yaml`, `session_duration`), and Envoy keeps the ID token
for 10 minutes before it returns to Authentik. So a person who leaves a
server keeps its folder for at most 24 hours and 10 minutes. A person who
joins a server sees its folder after their next Discord sign-in. A failed
server lookup gives no server groups until the next sign-in. To remove
access at once, remove the person from the group in Authentik, or end their
session there. Sign-in with GitHub or Google changes no server group.

```mermaid
flowchart LR
  A["Hermes actor"] -- "MCP put, placeholder" --> G["egress gateway"]
  G -- "actor JWT, aud bosun-artifacts" --> M["bosun-artifacts (factory-discord)"]
  M -- "bucket key" --> S[("Garage bosun-artifacts")]
  J["Joe's browser"] -- "Authentik sign-in" --> E["edge: CSP sandbox, nosniff, no cookies"]
  E -- "Host bosun-artifacts.web.garage.internal" --> W["Garage web :3902"]
  W --> S
```

| Control | Where |
| --- | --- |
| Only routed server agents' actors call the tools: actor JWT with audience `bosun-artifacts`, atespace in `ACTOR_JWT_ATESPACES` | `artifacts/mcp.go`, broker's `actorjwt` |
| Files go under `<server>/<channel>/` or `private/<channel>/`; the server must be a `bridge.policy.guilds` key or `private`, the channel a snowflake | `artifacts/artifacts.go` |
| Discord sign-in sets the `discord-<server-id>` groups (blueprint groups marked `discord_server_id`) from the `guilds` scope; it adds and removes them at each sign-in | Authentik `factory-social.yaml`, `factory-auth.yaml` |
| The edge token's `groups` claim holds only the groups the edge checks | Authentik `factory-auth.yaml` `factory-edge-profile` |
| One route rule and SecurityPolicy per server folder; each merges into the gateway sign-in and allows only its group and `authentik Admins`; default deny | edge `artifact-route.yaml` |
| Extensions `.html .md .txt .csv .json .svg .png`; the type comes from the extension; at most 2 MiB; PNG checked by magic bytes; Markdown served as plain text | `artifacts/artifacts.go` |
| Bucket quota 2 GiB and 20,000 objects; website access on this bucket and `bosun-libs` only | `garage_init.py` `WEBSITES` |
| Every response: `Content-Security-Policy: sandbox allow-scripts allow-popups` with fetch directives that allow only inline and `data:` content, and scripts from `https://artifact.jomcgi.dev/_lib/`, `nosniff`, `Referrer-Policy: no-referrer`, `CORP: same-origin`; `Set-Cookie` and `Access-Control-Allow-Origin` removed; `Cookie` and `Authorization` not sent to Garage | edge `artifact-route.yaml` |
| Only the edge proxy reaches Garage's port 3902; only the egress gateway reaches the service | Cilium policies |

The sandbox gives each page an opaque origin. Thus a page cannot read
cookies or storage, and it cannot load other artifacts. The fetch
directives (`default-src 'none'`, `connect-src 'none'`) stop a page from
reaching any origin, so it cannot send what it holds past the egress
gateway's scanner. An HTML artifact must inline its CSS and images, and
load scripts only from `/_lib/`; a CDN library does not load. The tools'
descriptions tell the model this. Residual: a page can still navigate
itself to another URL.

**Libraries at `/_lib/`.** The read-only `template` tool returns a design
brief (`artifacts/brief.md`), the base page (`artifacts/template.html`) and,
on request, one of three example pages (`artifacts/examples/`: explainer,
report, comparison). Every page loads the house design system
(`libs/house/house-2.css` and `house-2.js`) and the pinned
libraries, each with an `integrity` hash. The design system copies
jomcgi.dev's look as values (`jomcgi-site/web/src/palette.css`, `home.css`):
near-white or near-black page, hairlines, ink text, one blue accent, green
for good states, Schibsted Grotesk headings, Inter text, and housing panels
with a solid offset shadow. Restraint is the default, after Dieter Rams; one
`.statement` per page is the bold editorial moment, after Tracy Ma (the
design catalog in #8730). Components: hero, statement, stat tiles with a dark
readout, cards with Lucide icons, steps, timeline, callouts, comparison,
code, figures, and controls for a page to try (stage, buttons, segmented
control, toggle, readout). Page CSS uses only the house tokens. Mermaid
diagrams keep a 36rem minimum width and scroll sideways. Mermaid uses the
ELK layout: on
Joe's architecture diagram it kept every edge out of the group and off the
nodes, where dagre crossed a label and a node. A library file's name carries
its version, and old versions stay, so a page keeps the look it was
published with: `house-1` stays served for the pages that link it. The
`template` tool states the current house version. For an HTML page, `put`
returns warnings (`artifacts/lint.go`) and still stores the page: no house
link, an old house version, emoji, or its own colours in CSS, attributes,
Mermaid style lines or chart specs.

```mermaid
flowchart LR
  N[npm tarballs, sha256 in MODULE.bazel] --> I[connector image, /usr/share/bosun-libs]
  I -->|bosun-libs PostSync Job| B[(bucket bosun-libs)]
  B -->|route artifact-lib, no sign-in, GET| P[artifact page /_lib/...]
```

| Control | Where |
| --- | --- |
| Mermaid 12.3.0, Vega 6.4.0, Vega-Lite 6.5.0, Vega-Embed 7.3.0, Lucide 1.72.0 (ISC) and the Inter and Schibsted Grotesk fonts (OFL) from the npm tarballs, each pinned by sha256; the house files from the repository; the pages' sha384 `integrity` values are tested against the shipped files | `MODULE.bazel`, `libs/libs.bzl`, `libs/sri_test.py` |
| Only the `bosun-libs` Job holds the `bosun-libs` key; the artifact tools have none and still refuse `.js` and any server outside `bridge.policy.guilds` | `garage_init.py` `GRANTS`, `artifacts/artifacts.go` |
| `/_lib/` is public, GET only, rewritten to the `bosun-libs` bucket, with CORS and `CORP: cross-origin` so a sandboxed page can check the integrity hash, and the artifact CSP | edge `artifact-route.yaml` |
| `script-src`, `style-src` and `font-src` name exactly `https://artifact.jomcgi.dev/_lib/`; `'unsafe-eval'` is for Vega's expression compiler, and adds no reach because inline scripts already run | edge `values.yaml` |

`/_lib/selftest/architecture.html` is a reviewed self-test page: the
design system with Joe's architecture diagram and a chart.

**Deferred: screenshot service.** A headless Chromium pod that turns an
artifact into a PNG for Discord is deferred (HOME-909). Bosun posts
the artifact's link with a one-line summary.

**Trust limit.** The edge enforces the folders, but the caller picks the
folder. One homelab session answers Joe in all four servers
(`servers: all`), so the actor JWT names the homelab atespace for every
turn. The JWT does not carry the server or channel, so the caller states
them, and the server allowlist bounds them. A wrong or injected server ID
puts a file in another server's folder. This is safe while only Joe
triggers Bosun (`bridge.policy.users: operator`): Joe asked for the file
and reads where it went. Before Bosun answers other people, the bridge must
bind the server to the turn.

**Add a server.** Add the guild ID in all three lists, in one PR;
`//projects/platform/authentik:discord_server_groups_test` checks that they
match:

1. `projects/factory/deploy/values.yaml` `discord.bridge.policy.guilds`
   (a Joe-gated path).
2. `projects/platform/authentik/factory-blueprints/factory-auth.yaml`: a
   `discord-<id>` group with attribute `discord_server_id: "<id>"`, and its
   `edge-app` policy binding.
3. `projects/platform-factory/edge/chart/values.yaml` `artifacts.servers`.

Members get the group at their next Discord sign-in. To remove a server,
remove it from the bridge and edge lists, and set its group and binding
entries to `state: absent` (Authentik keeps an object whose blueprint entry
is only deleted). Its folder is then Admins-only.

**Share links (HOME-917).** A share link opens one file for anyone who
has it, with no sign-in, until it expires. Bosun cannot make one; Joe
confirms each link.

```mermaid
sequenceDiagram
  participant J as Joe
  participant B as Bosun (agent)
  participant R as Bridge
  participant E as Edge /s/
  participant G as Garage S3 API
  J->>B: share report.html for 3 days
  B->>R: answer with share block {path, ttl}
  R->>J: "Share <key> for 3 days? React ✅" (bot adds ✅)
  J->>R: ✅ on that message within 10 minutes
  R->>R: presign GET with the read-only share key
  R->>J: link, valid until <time> UTC
  Note over E,G: anyone with the link
  E->>G: GET /bosun-artifacts/<key>?X-Amz-..., Host kept
  G-->>E: file, 403 for a bad signature, 400 when expired
```

| Rule | Where |
| --- | --- |
| Only a fenced `share` block asks; the bridge takes one per answer. `path` is a file of the current channel, its URL or its key (`<server>/<channel>/…` or `private/<channel>/…`); `ttl` is 1 minute to 7 days, 24 hours by default | `share.go` `ParseShare`, `artifacts/share.go` `ShareKey` |
| A link needs Joe's ✅ (`Operator.UserID`) on that exact confirmation, in its channel, within 10 minutes. Other users, other emoji, the bot's own ✅ and late reactions do nothing. A confirmation makes one link | `share.go` `HandleReaction` |
| The bridge signs with `bosun-artifacts-share-s3`, a read-only key for the bucket that only the bridge mounts. The agent and the artifacts service never hold it | `garage_init.py` `GRANTS`, chart `discord.yaml` |
| The link is `/s/<key>?X-Amz-…`. The edge rewrites the prefix to `/bosun-artifacts/`, keeps Host, and allows GET and HEAD with no sign-in. Garage checks the signature and expiry: a bad or unsigned request gets 403, an expired link 400 ("Date is too old") | edge `artifact-route.yaml` route `artifact-share` |
| Each response gets the artifact sandbox CSP, `nosniff`, `no-referrer`, `CORP: same-origin`, `X-Robots-Tag: noindex, nofollow` and `Cache-Control: private, no-store`; `Set-Cookie` is removed | same |

The bridge posts the link in the channel, so the channel's members see it
too. A link to a missing file gets Garage's 404. The bridge does not check
that the file exists.

To revoke every link, rotate the share key, in this order, so that
garage-init cannot import the old key again:

1. Clear the Secret: `kubectl -n factory-discord patch secret
   bosun-artifacts-share-s3 --type=json -p '[{"op":"remove","path":"/data"}]'`.
   The next garage-init run mints a new key into it.
2. Delete the old key in Garage: `garage key delete --yes <old key ID>` on a
   Garage pod. From then on, every old link gets 403.

The bridge reads the key files at each link, so new links use the new key
when the kubelet updates the mount, with no restart.

**Wiring.** Deploy values bind it to the homelab agent
(`discord.serverAgents.homelab.mcpServers`, HOME-915): a RemoteMCPServer
`artifacts` in `discord-1501965852042330302` with URL
`http://bosun-artifacts.factory-discord.svc.cluster.local:8080/mcp` (the
cluster domain is required: Substrate resolves actor destinations as
absolute names), `actorJwt.audiences: [bosun-artifacts]` and tools `put`,
`template` and `list`. The egress gateway already reaches the service on 8080
(`../../platform-factory/substrate/resources/network-policy.yaml`).

### Visual style (HOME-909)

HOME-909 sets one visual style for Bosun. These parts hold it:

| Part | Where |
| --- | --- |
| One house theme for every Mermaid, Vega-Lite and SVG image: Inter, slate palette, color emoji, white background, chart text of 14 px or larger, a PNG about 2048 px wide. The agent cannot replace it. | `render/theme/`, `render/README.md` |
| The prompt's "Visual style" lines: inline blocks for quick images (one story, about 10 nodes at most, no groups, no emoji in Mermaid, Vega-Lite for data, drawings about 600 px wide, long Mermaid chains top to bottom), lists over tables, and for anything bigger an artifact from the template, posted as a link with a one-line summary | `chart/values.yaml` `systemPrompt` |
| The house template's diagrams and charts render in the browser from the pinned libraries at `/_lib/` | `artifacts/template.html`, `libs/` |
| The prompt's "Accuracy" line: no invented motives, quotes or facts about real people; a joke must be obvious | `chart/values.yaml` `systemPrompt` |

### Message history (HOME-909)

Hindsight returns extracted memories by similarity. It cannot find an exact
message or read the messages around one. So the bridge also keeps a
verbatim archive of every conversation message in the allowlisted servers,
and `bosun-history` gives Bosun four read tools on it.

```mermaid
flowchart LR
  D["Discord gateway"] -- "MESSAGE_CREATE / UPDATE / DELETE" --> B["discord-bridge"]
  H["history pages (backfill)"] --> B
  B -- "owner login: upsert, edit, delete, reconcile" --> P[("factory-db: factory_discord_archive")]
  A["Hermes actor"] -- "MCP history_*" --> G["egress gateway"]
  G -- "actor JWT, aud bosun-history" --> M["bosun-history (factory-discord)"]
  M -- "SELECT-only login (history_*)" --> P
  M -- "per-server login, row security (history_sql)" --> P
```

**What is stored.** One row per message (`archive/store.go`,
`discord_message`): server, channel and channel name, thread parent,
message ID, author ID and name (`username (display name)`), send time,
edit time, the replied-to message ID, a Bosun flag and the text. The text
has mentions named as in ingest (`mentions.go`) and placeholders such as
`[attachment: plan.pdf]`, `[embed: title]`, `[sticker: name]`. No file,
image or embed body is stored. A full-text index covers the text with the
`english` and `simple` configurations, and the author name.

**What is not stored.** Joe's operator channels and their threads, other
bots' and webhooks' messages (unless `backfill.includeBots`), system
messages, and servers off `bridge.policy.guilds`.

**Writes** (`archive.go`):

- Live: each `MESSAGE_CREATE` of a server with `ingest`, Bosun's replies
  included. Events go through a bounded queue (2,048) so the gateway never
  waits. A full queue drops the event and counts it.
- Edit: a `MESSAGE_UPDATE` with `edited_timestamp` replaces the text. An
  older edit never replaces a newer one.
- Delete: `MESSAGE_DELETE` and `MESSAGE_DELETE_BULK` mark the row deleted
  and clear its text and reply link. A delete of a message the archive does
  not hold yet leaves a tombstone, so a later history page cannot add it.
- Backfill: each history page the backfill reads (every pass, every
  server with `history`) is upserted by message ID, so a pass is idempotent
  and resumes where a stopped one ended. Archived messages in the page's ID
  range that Discord did not return are marked deleted. Thus a create or a
  delete that the live path dropped is repaired on the next pass (6 h).

**Read tools** (`archive/read.go`, `archive/mcp.go`):

| Tool | Arguments | Answer |
| --- | --- | --- |
| `history_search` | `server`, `query` (web search syntax), `author`, `channel` (ID includes its threads), `from`, `to`, `limit` (20, at most 50) | best match first with a query, else newest |
| `history_around` | `server`, `message_id`, `before`, `after` (10, at most 25 each) | the channel's messages around one, oldest first |
| `history_day` | `server`, `channel`, `date` (UTC), `after`, `limit` (100, at most 200) | one channel's day, oldest first; `next_after` continues it |
| `history_sql` | `server`, `query` (one PostgreSQL `SELECT`) | `columns`, `rows` (at most 2,000 and 60 kB), `truncated` |

Each message has its ID, channel, author, time, text and jump link. Text is
cut at 2,000 bytes per message and 30,000 per answer. `history_sql` is for
counts, top posters, activity over time, word frequencies (`ts_stat`) and
first mentions (Joe, 2026-11-28: SQL tool, not a sandbox with a database
URL). Its description lists the `discord_message` columns and example
queries; the prompt says to use its numbers and never estimate, and to draw
a word cloud as an artifact with Vega's `wordcloud` transform from `/_lib/`.
The query runs in a `READ ONLY` transaction with a 10 s statement timeout,
and is prepared, so PostgreSQL refuses a second statement. The first word
must be `SELECT`, `WITH`, `VALUES` or `TABLE`. A text cell is cut at 2,000
bytes. PostgreSQL's error message goes back to the model. Every answer carries
a note that the text is untrusted data. The log names the tool, server and
count, never the query or the text.

| Control | Where |
| --- | --- |
| Only routed server agents' actors call the tools: actor JWT with audience `bosun-history`, atespace in `ACTOR_JWT_ATESPACES` | `archive/mcp.go`, broker's `actorjwt` |
| A call reads one server, which must be in `bridge.policy.guilds` (`HISTORY_SERVERS`) | `archive/read.go` |
| The reader login `factory_discord_archive_reader` may only `SELECT` the table; the bridge's login owns it | factory-db `cluster.yaml`, `archive.Migrate` |
| `history_sql` runs on the server's own login `factory_discord_sql_<guild>`, a member of `factory_discord_sql` only. Row security on `discord_message` shows the group the rows whose guild is the one in `current_user`. A `SELECT` cannot change `current_user`: `set_config`, `SET ROLE` to another login and session settings do not help, and the group role itself sees nothing. `ts_stat` and `query_to_xml` run as the caller, and `pg_stats` hides the table. The catalogs (`pg_class.reltuples`, table size) still show the total row count of all servers, never their text. Tests: `archive/sql_test.go` | `archive.Migrate`, factory-db `cluster.yaml`, `bosun-history` `HISTORY_SQL_URL_<guild>` |
| Only the egress gateway reaches the service; only the bridge and the service reach factory-db from `factory-discord` | Cilium policies |

The trust limit is the one artifacts have: the actor JWT names the homelab
atespace for every turn and does not carry the server. The caller states
the server, and the allowlist bounds it. Today Bosun answers Joe alone, so
an answer from another server's archive goes to Joe. Before Bosun answers
other people, the bridge must bind the server to the turn.

**Retention and deletion.** Rows stay while the server is allowlisted.
They stay in the cluster: factory-db on local disks, and its 14-day base
backups and WAL in the in-cluster Garage bucket `factory-db-backups`. No
tool or job exports them. To delete a server's archive, remove it from
`bridge.policy.guilds`, add its ID to `discord.archive.purge` and deploy:
the bridge deletes its rows at start and logs the count. The bridge never
purges an allowlisted server. Backups keep the rows until they age out (14
days). Turn the whole archive off with `discord.archive.enabled: false`;
the rows stay until the database is dropped.

**Wiring.** Deploy values bind it to the homelab agent
(`discord.serverAgents.homelab.mcpServers`, HOME-915): RemoteMCPServer
`history` at `http://bosun-history.factory-discord.svc.cluster.local:8080/mcp`,
`actorJwt.audiences: [bosun-history]`, tools `history_search`,
`history_around`, `history_day` and `history_sql`. The tools prompt says
when to use history and when memories, and the Capabilities paragraph lists
it.

**Per-server logins.** A server added to `bridge.policy.guilds` needs its
history_sql login in four places: a managed role in factory-db
`cluster.yaml`, its login Secret and Role name in `resources/setup.yaml`,
`LOGINS` and `DATABASES` in `secret_init.py` (and its test), and the
`discord-sql-<guild>-db` placeholder and Role name in
`factory-db/discord/secrets.yaml`. `provisioning_test.py` checks that the
logins match the deploy values' servers. The bridge grants the group
`SELECT` and sets the policies at start.

### Research (HOME-909)

Bosun can research a public GitHub repository through an explorer agent
that clones it in a sandbox. Bosun writes a fenced `explore` block:
`{"repo": "owner/name", "question": "...", "research": "new"}`. For a
follow-up, `research` is the research ID that the thread shows. When Bosun
is not sure of owner/name, a new research can give `"repo_search": "words"`
in place of `repo`.

```mermaid
sequenceDiagram
  participant J as Joe
  participant B as bridge
  participant H as Bosun (Hermes)
  participant G as GitHub API
  participant E as explorer (Claude Code)
  J->>B: tag Bosun
  B->>H: turn
  H-->>B: answer + explore block
  B->>J: answer, research thread, "Researching ..."
  B->>G: GET /repos/owner/name, no credentials
  G-->>B: public, size
  B->>E: turn in session discord-explore-ID
  E->>E: git clone --depth 1 (github.com only)
  E-->>B: answer
  B->>J: answer in the thread
  J->>B: next tag in the thread
  B->>H: turn with research_results
```

- **Explorer.** Kagent Agent `explorer` in namespace and atespace
  `discord-explorer` (`chart/templates/discord-explorer.yaml`). It runs the
  unmodified Claude Code harness (the planner's image) with
  `claude-opus-5-5`. Model calls go through the broker's Claude relay with
  an actor JWT: the relay is live, the broker admits the atespace, and
  `broker.claude.modelAllowlist` lists `actor/discord-explorer/session-*`.
  The relay uses Joe's Claude subscription, so research does not stop when
  the Console key's credits end on 2026-12-03.
- **Egress.** EgressPolicy `public-github` admits `https://github.com`,
  `https://codeload.github.com` and `https://objects.githubusercontent.com`
  with no credential. The relay is the only other origin. The actor gets no
  GitHub token or placeholder, and git has no credential helper, so it can
  read only what GitHub serves to anyone and can push nothing.
- **Public check.** Before each explorer turn the bridge calls
  `GET https://api.github.com/repos/owner/name` with no credentials. It
  refuses unless GitHub answers 200 with `private: false` and `visibility:
  public`. The bridge's egress policy admits `api.github.com` for this.
- **Repository search.** For `repo_search` the bridge calls
  `GET https://api.github.com/search/repositories?q=<words> is:public` with
  no credentials and takes the first public result. The "Researching ..."
  message names that repository and the words. GitHub allows 10
  unauthenticated searches a minute: the bridge keeps each answer for 6 h
  and, while GitHub reports the limit spent, makes no search. With no match
  or no search, the bridge asks Joe for owner/name and spends no explorer
  turn. The public check then runs as for `repo`.
- **Validation.** The block must be strict JSON: `repo` in GitHub's
  owner/name syntax (no URL, no `.git`) or, for a new research only,
  `repo_search` of 1 to 100 letters, digits, spaces and `._/-` (no search
  qualifiers or quotes); a question of 1 to 2,000 characters; and
  `research` either `new` or a Discord snowflake.
- **Sessions.** One kagent session per research, with request ID
  `discord-explore-<research>` and the research ID as its name. A new
  research's ID is the ID of Joe's tagged message. The actor suspends 10 min
  after a turn; a follow-up restores it with its clone and context from the
  snapshot under `s3://ate-snapshots/discord-explorer/`.
- **Thread.** A new research starts a thread `Research: owner/name` from
  Bosun's reply. Inside a thread, Discord refuses a new thread, so the
  research stays in that thread. A follow-up posts where Joe asked.
- **Answer.** The bridge posts it as text that pings nobody and runs no
  action, in at most 3 messages of 3,800 characters, then clips it.
  Artifacts are an MCP tool of Bosun's actor; the bridge holds no actor
  JWT, so it cannot publish a long answer as an artifact.
- **Back to Bosun.** The bridge holds the last 3 answers per channel, in
  memory, for the research thread and the channel of the tag. Bosun's next
  turn there gets them as `research_results`, at most 6,000 characters
  each. The prompt and Bosun's system prompt say that they are untrusted
  data. A bridge restart drops held answers; the thread still shows them.
- **Bounds.**

  | Bound | Value | Where |
  | --- | --- | --- |
  | Explorer turns | 20 per hour, all research | `discord.explorer.limit` |
  | Turns at once | 2, one worker each | `concurrency` |
  | Turn time | 15 min | `timeout` |
  | One turn per research | a second gets "still running" | bridge |
  | Repository size | 512 MB by GitHub's `size`, shallow clone | `maxRepoMB` |
  | Sessions kept | 10; a new research deletes the oldest idle one | `maxSessions` |
  | Session TTL | 7 days after the last turn, swept each hour | `ttl` |

  The bridge keeps turn times in memory. After a restart its first sweep
  waits an hour, and a session with no turn since the restart counts its
  TTL from its creation.

### Formatting and events (HOME-909)

Bosun's replies are top-level Components V2 text displays and media
galleries, with no container: a container narrowed text on mobile and put
the white diagram images in a dark box (Joe, 2026-11-27). `REST.Post` runs
`Reply.Layout` (`layout.go`): a `---`, `***` or `___` line outside a code
block becomes a divider separator. Discord allows 40 components in a
message. When the parts pass that limit, the reply keeps its own text
displays and galleries. The 4,000-character limit and the image order stay
as before. The bridge does not escape text, so dynamic timestamps
(`<t:UNIX:F>`) show in each reader's time zone. `allowed_mentions` stays
`parse: []`, so no text pings. Polls are unchanged: a poll cannot carry
components.

Bosun creates a server event from a fenced `event` block:

```event
{"name": "Pub quiz", "start": "2026-12-04T19:30:00+00:00",
 "location": "The Crown", "description": "Teams of four"}
```

`channel` (a voice channel name) replaces `location`; `end` is optional.
The bridge takes the first valid block and leaves others as text. It
refuses a block with unknown fields, a time without an offset, both or
neither place, a name or location over 100 characters, a description over
1,000, an end before the start or over 7 days later, or text with
`@everyone`, `@here` or a `<@…>` or `<#…>` mention. At turn time it
refuses a start in the past or over a year ahead. A location event without
an end lasts one hour, because Discord needs an end for an external event.

The bridge creates a server-only event (`privacy_level` 2) through
`POST /guilds/{id}/scheduled-events` in the message's server. Then it adds
`📅 name · <t:start:F>` and `https://discord.com/events/{server}/{event}`
to the answer. For a voice event it finds the one voice channel with that
name. A refusal logs the reason and adds a `-# I couldn't create the
event: …` line. Memory records `[event: name, start]`.

Permissions Joe grants the bot's role in each server:

| Permission | For |
|---|---|
| Create Events | create an event |
| View Channel, Connect | a voice channel event, on that channel |

Manage Events also lets the bot create events. Without a permission,
Discord answers 403 and the bridge logs `event refused: the bot lacks the
Create Events permission`.

### Gloves off (HOME-938)

Joe can turn one channel to a second agent for plain chat that the normal
model's filter refuses (Joe, 2026-11-28). People see "gloves off"; code and
config say `unsafe`. `unsafe.go` holds the bridge side.

```mermaid
stateDiagram-v2
    [*] --> GlovesOn
    GlovesOn --> GlovesOff: Joe tags "take your gloves off [for 1h]"
    GlovesOff --> GlovesOn: Joe tags "gloves on"
    GlovesOff --> GlovesOn: timer (bridge posts gloves-on GIF)
    GlovesOff --> GlovesOn: bridge restart (no message)
```

- **Trigger.** Only Joe's tag. The bridge matches words, not a model: the
  message has 12 words or fewer, all from a small command vocabulary, with
  "gloves" (or 🥊) and "off" or "on". A duration such as "1h", "90 min" or
  "half an hour" is optional: 30 min by default, 4 h at most.
- **Agent.** `discord.serverAgents.unsafe`: namespace `discord-unsafe`,
  abliteration.ai's Messages API (`abliterated-model-large-v2`), key from
  1Password item `factory-discord-abliteration`. It has no MCP servers, no
  server tools and no memory; the chart fails a render that gives it any. It
  is never routed, so the artifact, history and GitHub tools refuse its
  actor. Each gloves-off window has its own session.
- **Silo.** An unsafe turn has no recall, no retain and no ingest. The
  archive skips it. Every reply ends with
  `-# 🥊 gloves off · gloves back on <t:unix:R>`. History readers (the
  normal turn's channel context, the archive backfill, the Hindsight
  backfill) drop a Bosun message with that footer and the message it
  answers.
- **Restart.** A restart ends gloves off with no message. The catch-up
  after it leaves unanswered a tag of Joe's that Bosun's notices place in
  gloves off; it reads one page before its window to find the notice.
- **Kept where.** "No memory" means no Hindsight, archive or ingest. The
  agent's own sessions stay in kagent's session store and in its
  snapshots (`s3://ate-snapshots/discord-unsafe/`), apart from Bosun's.
- **Egress.** Workers may reach `api.abliteration.ai`; the egress gateway
  scans each request body (`gateway-policy-tls.yaml`) before it injects
  the key.
- **Offer after a block.** When the normal model's filter blocks Joe's
  message, the bridge (`offer.go`) answers with a playful line, the fixed
  "React 🥊 if you want me to take my gloves off." and its own 🥊, in place
  of the blocked reply. The gloves-off model writes the line in one
  stateless call: a fresh session, deleted after, that gets only the
  blocked text. After 5 s, an error or an empty answer, a canned line
  stands in. The offer carries the gloves-off footer marker, so history
  readers drop it and the blocked message. Joe's own 🥊 within 10 min
  turns gloves off (default time) and replays the message as a gloves-off
  turn that answers it. The bridge keeps the message in memory only until
  the offer ends. Without gloves off, or for another author, the reply
  stays the blocked one.
- **GIFs.** `gloves/gloves-off.gif` and `gloves/gloves-on.gif` are
  embedded in the bridge. To use other files, replace those two files.
- **Billing.** Prepaid credits only, with auto-reload off.

## 5. Model and token cost

**Recommendation.** A model per server in values, a hard token budget per
turn, and one memory owner per kind of fact.

- **Model.** `discord.serverAgents.<key>.model` and `provider`. `openAI`
  renders an OpenAI-compatible ModelConfig (OpenRouter); `anthropic`
  renders an Anthropic one, which the kagent fork (jomcgi-org/kagent#5)
  writes as a Hermes provider with `api_mode: anthropic_messages`. The
  actor holds a placeholder; the egress gateway injects the key as
  `x-api-key` and admits only `api.anthropic.com`.
- **Homelab agent (Joe, 2026-11-27).** `claude-opus-5-5` answers and
  `claude-sonnet-5-5` writes the compaction summaries, both on Joe's
  Anthropic Console key (item `factory-discord-anthropic`). Prompt caching
  is on with a 1 h TTL (`cacheTTL: 1h`), which matches the 1 h idle
  compaction.
- **Credits end 2026-12-03 00:00 GMT.** The key spends about $196 of
  promotional credits. A Console organization prepays: at zero balance the
  API refuses requests, and the bridge replies "I couldn't answer that
  right now". A card is charged only if auto-reload is on for the
  organization; keep it off. Before 2026-12-03, move the agent to a plan
  path or another model by PR.
- **Budget.** Each turn has a cap on output tokens and on tool calls (about
  4). Hermes resends the whole session on each turn. Without a
  `compression` key it compacts only at half the window, which is 500k
  tokens on a 1M-token model. The Harness sets
  `spec.hermes.compression` (kagent fork, jomcgi-org/kagent#4):

  | Setting | Value | Effect |
  | --- | --- | --- |
  | `thresholdTokens` | 256,000 | Size backstop: compact when the prompt reaches it |
  | `minTailUserMessages` | 3 | The last 3 user messages stay verbatim |
  | `model` | `claude-sonnet-5-5` | Summary model, through the same `kagent` provider and key |

  Cost on Opus 5.5 ($4/M input, $8/M 1 h cache write, $0.20/M cache read,
  $20/M output): a warm turn near 256k reads the cached prefix for about
  $0.05. A cold turn near 256k writes it again for about $2. Each
  compaction rewrites the prefix, so it costs one cache miss on the next
  turn, plus the Sonnet summary ($2/M input), about $0.50 for a 256k
  session. A low trigger, such as 32k,
  would compact almost every turn, because Hermes keeps a 25k-token tail
  on a 1M window. That breaks caching.

  Joe chose an idle trigger too: compact when the session resumes after 1 h
  idle, when the cache is cold anyway. Hermes 0.23.4
  `compression.idle_compact_after_seconds` does not fire on this path. The
  API server builds a new agent for each request, so the idle gap is
  always about 0 s. An adapter-side trigger is a follow-up. Per-turn cache
  hits (`cached_tokens` against prompt tokens, seconds since the last turn)
  are not logged yet: Hermes's `/v1/responses` usage has no cache fields.
  That is a follow-up too, to tune the 1 h value against the real cache
  lifetime.

  A per-turn input cap in Hermes is not confirmed, so the key's balance
  (or the per-server OpenRouter limit) is the hard bound. The bridge's existing limits still bound
  turns per user, server and day.
- **Memory split.**

  | Memory | Owner | Holds |
  | --- | --- | --- |
  | Session context | Hermes, compacted | The recent turns in this server |
  | Agent notes | Hermes `MEMORY.md` and `USER.md` in the notes vault, per server (HOME-921) | The agent's own durable notes and facts about Joe, at most 2,200 and 1,375 characters |
  | Server history | Hindsight, bridge ingest | Every human message, server-wide |

- **No double memory.** For a server with an agent, the bridge stops putting
  recalled memories into the prompt. Hermes' Hindsight provider recalls
  instead (`auto_recall: true`, `recall_budget` bounds it).
  `memory_mode: context` keeps the `hindsight_retain`, `hindsight_recall`
  and `hindsight_reflect` tools out of the model's tool list; the default
  `hybrid` exposes them, and `auto_retain: false` alone leaves retain. The
  extension refusing retain is the control, so Hermes never writes a
  second copy of Discord history. Compaction keeps recent turns; Hindsight answers about anything
  older.
- **Agent notes.** `MEMORY.md` loads into every session's system prompt
  ([memory](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/memory.md)), and people other than Joe
  write the turns. Joe turned the memory tool on with the files in the
  notes vault ([decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-921#comment-12939));
  the prompt rule in "Tool security" keeps other people's text out.

## 6. Migration from Bosun and HOME-162 rule changes

**Recommendation.** Move the homelab server first; the other three stay on
Bosun until slice 6's measures pass. The chart's
`discord.serverAgents.<key>.route: true` adds the server to the policy's
`serverAgents`; without it the server stays on Bosun. Rollback is
`route: false`.

Joe confirms each change below. Rules 1, 2, 3, 5, 6, 7 and 8 are worded
from the code and chart comments that cite them. Rule 4 is inferred from
the operator exception in `operator.go`; Joe checks it against HOME-162.

| Rule | Old | New |
| --- | --- | --- |
| 1 | Discord text reaches the agent as data, in one JSON value | Same |
| 2 | One dedicated model key for Bosun | One dedicated key per server agent, each with its own limit |
| 3 | The agent has no tools; the bridge is the only Hindsight client | Per-server MCP allowlist, read-only by default; the agent recalls its own bank through the gateway |
| 4 | A turn's context is the tagged message and its channel's memory | The server's persistent session also carries earlier turns in that server |
| 5 | Recall reads one channel's tag (ingest already amended retain, HOME-833) | Recall is server-wide inside the server's bank (HOME-904). Decided: Joe chose this on 2026-11-26, told that it can show private-channel text to members who cannot see that channel |
| 6 | Replies ping nobody | Same |
| 7 | User, guild and global turn limits | Same, plus a token and tool-call budget per turn |
| 8 | Server allowlist; one bank per server | Same, plus one atespace and identity per server |
| "Short sessions" | A fresh session per turn, deleted after | One persistent session per server, suspended when idle |

## 7. Measures

The bridge records each measure per server. Slice 6 compares the homelab
agent with Bosun's baseline from the same week.

| Measure | Source | Target |
| --- | --- | --- |
| End-to-end time, tag to reply, p50 and p90 | Bridge timing log (slice 6) | p50 at most Bosun's; p90 at most 12 s |
| Wake time: restore to first model byte | Bridge timing log (slice 6) | p90 at most 2 s |
| Suspend time | kagent controller log | p99 at most 60 s |
| Input and output tokens per turn | OpenRouter usage per key | Median input at most 2 times Bosun's |
| Tool calls per turn | Hermes or MCP logs | Median at most 1 |
| Answer quality | 10 fixed questions per server, graded pass or fail | At least Bosun's score |

A server's question set holds 5 questions about its own history (the answer
is in its bank) and 5 general questions. The homelab set adds 3 questions
that need factory MCP. The grader and the questions live beside the bridge
tests; no Discord user text goes into the repository.

## 8. Rollout

Each slice is one sub-ticket of HOME-905 and one PR. Slices 1 to 6 touch
the homelab server only.

| Slice | Title | Done when |
| --- | --- | --- |
| 1 | Build a Hermes harness image for kagent | An A2A turn through kagent gets a Hermes answer in CI, with Discord and local tools off |
| 2 | Run a persistent homelab agent on ATE | The homelab session suspends when idle, writes an object to `ate-snapshots` and restores on the next turn |
| 3 | Scope Hindsight recall to the actor's server | The homelab actor recalls bank `discord-1501965852042330302`; another bank, retain and reflect fail; the actor JWT binds the origin, or else a per-guild injectable token does |
| 4 | Route homelab turns to its agent | A tag in the homelab server answers from the agent; the other three servers still answer from Bosun |
| 5 | Give the homelab agent factory MCP read tools | The agent answers a run-status question; a write tool call fails |
| 6 | Measure the homelab agent against Bosun | p50, p90, tokens and the question-set score are recorded on HOME-905 for 7 days |
| 7 | Move the other servers one at a time | Each server meets slice 6's targets, then Bosun's Agent is removed |

## Open decisions

Each decision names the rule that needs Joe.

| Decision | Rule | Options, recommended first |
| --- | --- | --- |
| Gateway owner | HOME-162 rules 1, 6, 7, 8 | Bridge keeps the gateway; Hermes owns it per server |
| HOME-162 rule changes | HOME-162 is Joe's contract | Accept the table in section 6; accept it without rule 3 (no agent tools yet); keep Bosun |
| Model key per server | HOME-162 rule 2 | One OpenRouter key and limit per server; one shared key with bridge limits only |
| Recall as a tool | HOME-915, "Recall as a tool" | Bind the bank to the session at the gateway; one Agent per server; bind now with the prompt rule only |
| Private channels in recall (follow-up) | HOME-904 | Tag non-public channels at ingest and leave them out of server-wide recall; keep server-wide recall as decided |
| Session scope | HOME-162 rule 4 | One session per server; one session per channel (one actor each) |
| First server | HOME-905 scope | homelab only; homelab and Loom together |

## Hermes facts

Read on 2026-11-26 from the repository's `main` branch
([docs tree](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/)). Not confirmed in the docs:

- A Discord server (guild) allowlist in the Hermes adapter.
- A per-server placeholder in Hindsight's `bank_id_template` (`{chat}` and
  `{platform}` exist). The design sets `bank_id` per actor instead.
- A per-turn input-token cap.
- Which kagent resource binds the hindsight-discord origin to an actor JWT
  (only ModelConfig has `actorJwt` here).
- Resource needs outside Docker (the Docker page states 1 GB and 1 core
  minimum, 2 to 4 GB and 2 cores recommended).
- Whether the `discord` toolsets work without the native adapter.
- How large `state.db` grows per month of turns, which sets checkpoint time.

Slice 1 (HOME-906) found two facts in the v0.23.4 source:

- Hermes' A2A endpoint is JSON-RPC over HTTP. kagent's BYO contract is
  private A2A gRPC with the control-plane TaskStore and a bootstrap bundle,
  so the two do not match.
- On `/v1/responses`, `X-Hermes-Session-Key` alone does not replay earlier
  turns (`history_from_session` stays false). A `conversation` name chains
  each turn to the last stored response.
