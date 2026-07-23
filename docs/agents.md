# Agent Platform

This document describes the agent stack end-to-end: how **goosecracker** turns a
Discord slash command into a goose run, how that run executes inside an isolated
Firecracker microVM behind the **fc-invoke** daemon, and how the result (an
answer, a PR, or a live-hot-reloading artifact) gets back to the thread.

The whole stack lives in two places:

- **Orchestration + integration** in the monolith (`projects/monolith/goosecracker/`,
  `projects/monolith/chat/`, `projects/monolith/artifact/`). Trigger, gate, session
  ledger, tiers, progress buffer, result delivery, artifact publish/serve.
- **Execution substrate** in `projects/firecracker/` (`substrate/` = the fc-invoke
  daemon + Firecracker driver + egress proxy; `goosecracker/` = the guest image and
  recipes; `git-mirror/` = fast workspace hydration).

> **Legacy note.** Earlier versions of this doc described an `agent-orchestrator`
> (Go, NATS JetStream) and the `kubernetes-sigs/agent-sandbox` controller with
> `SandboxClaim` / `SandboxWarmPool` CRDs. Both are gone: the orchestrator is
> decommissioned (only a stale UI dir remains under `projects/agent_platform/`),
> and the pod-shaped agent-sandbox controller was rejected in
> [ADR 022](decisions/agents/022-firecracker-snapshot-restore-controller.md) in
> favour of driving Firecracker directly. See [Superseded Architecture](#superseded-architecture)
> at the end.

## Component Map

```
Discord  (owner types /artifact <prompt> or /agent <prompt>)
    │
    ▼
┌────────────────────────────────────────────────────────────────────────────────────────────┐
│  monolith / chat  (projects/monolith/chat/bot.py + goosecracker.py)                        │
│  Owner gate (OWNER_DISCORD_USER_ID, fails closed) · non-owner gets a Qwen roast            │
│  Opens a Discord thread, curates the transcript (session id == thread id)                  │
└──────────────────────────────────────────┬─────────────────────────────────────────────────┘
                                           │  submit(task, session, recipe, tier)
                                           ▼
┌────────────────────────────────────────────────────────────────────────────────────────────┐
│  monolith / goosecracker  (dispatch → runner → threads/sessions/tiers)                     │
│  Writes the run-ledger row, fires a detached task, POSTs the goose run to fc-invoke        │
│  Streams progress into an in-memory buffer · enqueues the result to the Discord outbox     │
└──────────────────────────────────────────┬─────────────────────────────────────────────────┘
                                           │  POST /invoke/agent/{session}   (HTTP)
                                           ▼
┌────────────────────────────────────────────────────────────────────────────────────────────┐
│  fc-invoke daemon  (projects/firecracker/substrate · node-4 · namespace monolith)          │
│  ADR 030/031 · one configurable surface for Firecracker workloads                          │
│  Restores a warm-base microVM (~tens of ms) · reverse-proxies HTTP over vsock to the guest │
│  Egress proxy swaps kloak: secret placeholders at the egress hop (ADR 023)                 │
└──────────────────────────────────────────┬─────────────────────────────────────────────────┘
                                           │  HTTP over vsock
                                           ▼
┌────────────────────────────────────────────────────────────────────────────────────────────┐
│  Guest microVM  (projects/firecracker/goosecracker/guest · apko image, uid 65532)          │
│  Hydrates /workspace from the git mirror (ADR 041) · runs goose --recipe <recipe>          │
│  Recipes: agent (router → query/plan/implement) · artifact (build one HTML file)           │
│  Model: in-cluster Qwen (default tier) or Gemini via OpenRouter (artifact tier)            │
│  Streams goose stdout to the progress endpoint · publishes artifact HTML to the monolith   │
└────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 1. goosecracker: Trigger, Gate, Session

**Source:** `projects/monolith/chat/goosecracker.py` (gate + transcript + dispatch glue),
`projects/monolith/chat/bot.py` (Discord bot + slash commands),
`projects/monolith/goosecracker/` (dispatch, runner, ledger, tiers).

goosecracker is the owner-gated Discord agent introduced in
[ADR 024](decisions/agents/024-discord-agent-hosted-model-tiers-and-artifacts.md).
It is not a standalone service: it lives inside the monolith and reuses the
monolith's Discord bot, Postgres, and S3.

### Slash commands

Two application (slash) commands are registered on the bot's `CommandTree`
(`bot.py` `_register_commands`):

| Command              | Guest recipe | Deliverable                                                                  |
| -------------------- | ------------ | ---------------------------------------------------------------------------- |
| `/artifact <prompt>` | `artifact`   | A self-contained HTML artifact, published and served live (hot-reloading)    |
| `/agent <prompt>`    | `agent`      | An answer, a plan doc, or a PR (the router classifies) against a chosen repo |

`/agent` takes a `repo` choice (`homelab` or `loom`) selecting which repo the
guest checks out. Each command opens a Discord thread; **the thread id is the
session id** for the whole conversation.

### Owner gate

Access is a server-side allowlist of exactly one identity:

```python
# projects/monolith/chat/goosecracker.py
def is_owner(user_id) -> bool:
    owner = os.environ.get("OWNER_DISCORD_USER_ID", "")
    return bool(owner) and str(user_id) == owner  # fails closed if unset
```

A non-owner does not silently fail: the bot asks the in-cluster Qwen model to
generate a short roast ("Nice try, /artifact is owner-only"), falling back to a
fixed string when the model is down. There is no role or team model here, unlike
the old Context Forge RBAC. One owner, everyone else refused.

### Session model (Model B)

Each owner follow-up in a thread re-runs goose. The current implementation is
**Model B**: every turn re-runs goose from scratch with the full _curated_
transcript (the owner's directed messages plus goose's own outputs, not ambient
thread chatter). This keeps the artifact id stable across turns, so the published
artifact hot-reloads in the browser instead of moving. It is simple and fast at
the cost of not preserving goose's mid-run reasoning across turns.

Model A (resume goose's prior `sessions.db` from S3 and send only the new
message) is the future path described in
[ADR 026](decisions/agents/026-fast-microvm-starts-and-stateful-artifact-iteration.md)
Phase 2; the guest already checkpoints its SQLite WAL into `sessions.db` for it.

---

## 2. Dispatch and Delivery

**Source:** `projects/monolith/goosecracker/{dispatch,runner,threads,sessions,tiers}.py`

The dispatch seam is trigger-agnostic on purpose: Discord is the only trigger
today, but an MCP tool or a CI hook can call the same `submit()`.

```
submit(task, session, recipe, tier)               # dispatch.py
    │  write run-ledger row (PENDING)  ·  fire detached task  ·  return immediately
    ▼
run_and_deliver()                                  # runner.py  (detached daemon thread)
    │  POST {FC_INVOKE_URL}/invoke/agent/{session}  {task, recipe, env: <tier> }
    │  (a single blocking round-trip, up to ~600s: the whole goose run)
    ▼
on return:  mark ledger row  ·  enqueue result to the Discord outbox
    ▼
the bot drains discord_outbox and posts the summary / PR link / artifact URL to the thread
```

- **Detached execution.** `submit()` is called via `asyncio.to_thread` (a sync DB
  write must not run on the gateway event loop), so `runner` runs the
  ~600s fc-invoke round-trip to completion in a dedicated daemon thread and never
  blocks the Discord bot.
- **`FC_INVOKE_URL`** is injected from Helm values and is the same in-cluster
  fc-invoke daemon the `semgrep_scan` MCP tool uses. The workload name in the path
  is `agent` (`/invoke/agent/{session}`).
- **Result shape.** The `agent` recipe returns a structured result (summary,
  optional details, mode, type of pr/issue/note/answer, and a real `url` only if
  the worker opened one). The runner posts the summary and, for `/artifact`, a
  clean `Artifact ready: <url>` line.

### Live progress

While goose runs, the guest streams its stdout back through the fc-invoke egress
funnel to the monolith's own progress endpoint:

```yaml
# projects/monolith/deploy/values.yaml
goosecracker:
  progressUrl: "http://monolith.monolith.svc.cluster.local:8000/internal/goosecracker/progress"
```

The runner hands the guest `<progressUrl>/<session>`; the guest POSTs `{chunk: ...}`
frames there; the monolith keeps a per-session in-memory rolling buffer
(`chat/goosecracker_progress.py`); the Discord bot polls that buffer and edits the
thread message so the owner watches goose think in near-real-time. The buffer is
keyed by session (== the Discord thread id).

### Run ledger

Every run writes a row to the `claude_agent` run-ledger (schema in
`projects/monolith/chart/migrations/`), tracking `session`, `state`, `recipe`,
`tier`, `task`, and the thread it fronts. State is owned entirely by the monolith;
fc-invoke is stateless (see [ADR 030](decisions/agents/030-fc-invoke-configurable-firecracker-surface.md)).

---

## 3. Recipes: the Sub-Recipe Router

**Location:** `projects/firecracker/goosecracker/guest/recipes/`

Recipes are goose recipe YAML baked into the guest image. There are two entry
recipes plus three sub-recipes the router dispatches to.

| Recipe           | Version | Role                                                                                        |
| ---------------- | ------- | ------------------------------------------------------------------------------------------- |
| `agent.yaml`     | 1.3.0   | **Router.** Classifies the task and dispatches exactly one sub-recipe. Does no work itself. |
| `query.yaml`     | 1.2.0   | Read-only. Answers a question, grounded in `.claude/CLAUDE.md` + the project's CLAUDE.md.   |
| `plan.yaml`      | 1.2.0   | Writes an implementation plan and opens a PR on a `claude/-` branch.                        |
| `implement.yaml` | 1.2.0   | Autonomous coding: reads, edits, commits, pushes, opens a PR on a `claude/-` branch.        |
| `artifact.yaml`  | 1.0.0   | Writes ONE self-contained HTML file; the harness publishes it. Used by `/artifact`.         |

### Why a router (`agent.yaml`)

`/agent` runs the router, which classifies the incoming task into one mode and
delegates:

- **query**: a question ("how does X work", "why did Z fail"). Deliverable is an answer.
- **plan**: a design or a large/ambiguous change. Deliverable is a plan document.
- **implement**: a concrete, actionable change. Deliverable is a commit and a PR.

The router runs on the in-cluster Qwen model with a **small** context window, so
it is disciplined about not doing the work itself: it skims the repo's
`.claude/CLAUDE.md`, locates the relevant project directory, notes prior thread
state, and passes a short briefing into the sub-recipe's `context` param. A large
tool output in the router (a full `git log`, a wide `grep`) would overflow its
shared context and trigger a compaction that drops the answer mid-run, so
context-gathering is a couple of small, bounded reads only. The heavy reading is
the sub-recipe's job.

On a resumed thread the router classifies the _new_ message: a follow-up question
after an implement run routes to `query`; "now do it" after a plan run routes to
`implement`.

### `artifact.yaml`

The `/artifact` recipe is deliberately narrow: write one complete, self-contained
HTML file and nothing else. No action statements, only the file write counts.
Constraints: https-only resources, self-contained or CDN libs. The harness
auto-publishes the file after execution and retries if it comes back empty.

---

## 4. Tiers and Model Routing

**Source:** `projects/monolith/goosecracker/tiers.py`, values in `projects/monolith/deploy/values.yaml`

A **tier** is the guest environment for a run: the model endpoint plus the secret
_placeholders_ the guest is allowed to hold. The runner merges the selected tier
into the env it POSTs to fc-invoke. Tiers are injected from Helm values (no code
change to add or retune one).

| Tier       | Provider / endpoint                                            | Model                     | Model auth                                |
| ---------- | -------------------------------------------------------------- | ------------------------- | ----------------------------------------- |
| `default`  | `openai` → `inference.inference.svc.cluster.local:8080` (Qwen) | `qwen3.6-27b`             | `sk-noauth` (in-cluster, no real secret)  |
| `artifact` | `openrouter` (OpenRouter's own API endpoint)                   | `google/gemini-3.5-flash` | `OPENROUTER_API_KEY` `kloak:` placeholder |

Both tiers also carry a `GITHUB_TOKEN` `kloak:` placeholder so `plan` / `implement`
runs can push branches and open PRs, plus `GOOSE_MODE: auto` (the sandboxed guest
needs no interactive approval) and OTLP trace env.

```yaml
# projects/monolith/deploy/values.yaml (excerpt, trimmed)
goosecracker:
  tiers:
    default: # in-cluster Qwen; model needs no real secret
      OPENAI_HOST: http://inference.inference.svc.cluster.local:8080
      OPENAI_API_KEY: sk-noauth
      GOOSE_PROVIDER: openai
      GOOSE_MODEL: qwen3.6-27b
      GOOSE_CONTEXT_LIMIT: "32768" # true vLLM window; goose compacts at ~80%
      GOOSE_MAX_TOOL_RESPONSE_SIZE: "10000" # cap one tool output so it cannot fill 32k
      GOOSE_MAX_TOKENS: "8000" # cap per-response generation
      GITHUB_TOKEN: "kloak:gh:..." # swapped at egress on api.github.com
      GOOSE_MODE: auto
    artifact: # Gemini via OpenRouter
      GOOSE_PROVIDER: openrouter
      OPENROUTER_API_KEY: "kloak:or:..." # swapped at egress on openrouter.ai
      GOOSE_MODEL: google/gemini-3.5-flash
      GOOSE_MAX_TOKENS: "32000" # Gemini thinking tokens count against this budget
      ARTIFACT_PUBLISH_URL: http://monolith.monolith.svc.cluster.local:8000/internal/artifact
```

**The tier is the credential trust boundary.** No tier puts a real secret in the
guest. The `default` tier's model auth is `sk-noauth` because the model runs inside
the cluster; the `artifact` tier reaches OpenRouter but holds only an inert
`OPENROUTER_API_KEY` placeholder. Both tiers also carry a `GITHUB_TOKEN`
placeholder. Every `kloak:` value stays inert until the fc-invoke egress proxy
swaps it for the real secret (from the `goosecracker` 1Password item) at the egress
hop, on the destination host only
([ADR 023](decisions/agents/023-egress-secret-proxy.md)). A compromised guest leaks
a placeholder, not a credential.

The `GOOSE_CONTEXT_LIMIT` / `GOOSE_MAX_*` knobs on the `default` tier exist
because the Qwen endpoint's real window is 32k: without telling goose the true
window it assumes 128k, never compacts, and a big tool output overruns the model
and sends goose into a reactive summarize loop.

---

## 5. fc-invoke: the Execution Substrate

**Source:** `projects/firecracker/substrate/`
**Chart:** `projects/firecracker/substrate/chart/` · **Deploy:** `projects/firecracker/substrate/deploy/`
**Namespace:** `monolith` · **Node:** `node-4` (privileged, `/dev/kvm`)
**ADRs:** [030](decisions/agents/030-fc-invoke-configurable-firecracker-surface.md) (the surface),
[031](decisions/agents/031-cluster-node-control-data-plane-split.md) (control/data-plane split),
[022](decisions/agents/022-firecracker-snapshot-restore-controller.md) (FC-direct snapshot/restore)

fc-invoke is a single configurable HTTP daemon that runs _any_ workload inside a
Firecracker microVM. It replaced the earlier one-daemon-per-workload approach:
the retired `semgrep-scand` and `fc-agentd` daemons are both absorbed into it. It
drives Firecracker processes directly (as E2B does), not through a kata-fc shim,
because kata exposes no snapshot/restore API.

### Surface

```
POST /invoke/{workload}[/{session}]     run a workload; optional session for resume/routing
GET  /healthz
```

The request body is opaque to the daemon: fc-invoke restores the workload's
warm-base microVM and reverse-proxies the HTTP request over **vsock** to a server
running inside the guest. The daemon knows nothing about goose, semgrep, or
recipes; the guest owns all of that.

### Workloads are Helm values

A workload is a named entry in the substrate chart's values with a small set of
generic knobs (no per-workload code in the daemon):

| Knob             | Meaning                                                    |
| ---------------- | ---------------------------------------------------------- |
| `image`          | The guest OCI image (apko-built)                           |
| `resources`      | vCPU / memory for the microVM                              |
| `concurrency`    | Max concurrent invocations                                 |
| `egress`         | `{enabled, secrets}`: which `kloak:` placeholders to swap  |
| `warmBase`       | `{build, readyPath}`: snapshot prep + readiness probe path |
| `sessioned`      | Whether the workload takes a `{session}` path segment      |
| `requestTimeout` | Per-invoke deadline                                        |

Today's workloads: **`agent`** (goosecracker's goose runner) and **`semgrep`**
(the diff scanner behind the `semgrep_scan` MCP tool). The guest hydrates its
workspace from the separate **git-mirror** service.

### Warm-base restore

Each workload has a platform VM snapshot restored per invoke in tens of
milliseconds (~28ms measured in the ADR 022 spike), so a run does not pay a cold
boot. The guest is disposable: it is discarded after the invoke returns.

### Egress and secret swap

The egress proxy sits on the host side of the guest's network funnel. The guest's
network is default-deny to the cluster with an explicit allowlist, and `external`
egress is allowed but inspected so `kloak:` placeholders can be swapped for real
secrets before the request leaves the node:

```yaml
# projects/firecracker/substrate/deploy/values.yaml (excerpt)
egress:
  enabled: true
  external: allow
  internal:
    default: deny
    allowlist:
      - inference.inference.svc.cluster.local:8080 # in-cluster model (default tier)
      - monolith.monolith.svc.cluster.local:8000 # progress + artifact publish sink
      - context-forge-gateway-mcp-stack-mcpgateway.mcp.svc.cluster.local:80 # MCP tools
      - signoz-k8s-infra-otel-agent.signoz.svc.cluster.local:4318 # OTLP traces
      - git-mirror.monolith.svc.cluster.local:9418 # hot git mirror (ADR 041)
```

### Git mirror and session hydration

- **Workspace.** The guest clones `/workspace` from the in-cluster git mirror
  (`git://git-mirror.monolith.svc.cluster.local:9418`, from
  `projects/firecracker/git-mirror/`). This decouples repo freshness from VM base
  freshness ([ADR 041](decisions/agents/041-hot-git-mirror-agent-workspaces.md)):
  the microVM snapshot can be old while the checkout is current. The runner
  defaults the mirror to `<gitMirror>/homelab` when the caller does not specify a
  repo.
- **Session (Model A path).** The guest checkpoints goose's SQLite WAL into
  `sessions.db` and can ship it to S3 for resume; the `sqlite` CLI is baked into
  the image for exactly this (goose does not checkpoint on exit).

---

## 6. Guest Image

**Built with:** apko + rules_apko (`projects/firecracker/goosecracker/guest/apko.yaml`)
**Config:** `projects/firecracker/goosecracker/guest/config.yaml` (goose extensions)
**Architectures:** x86_64 + aarch64 · **User:** uid/gid 65532 · no Dockerfile

Wolfi packages baked in:

| Package(s)                         | Purpose                                        |
| ---------------------------------- | ---------------------------------------------- |
| `goose`                            | Agent framework (via recipes; entrypoint)      |
| `go`, `nodejs`, `pnpm`             | Build/test the target repos                    |
| `git`, `gh`, `openssh-client`      | Clone from the mirror, push branches, open PRs |
| `sqlite`                           | Checkpoint goose's WAL into `sessions.db`      |
| `bash`, `busybox`, `coreutils`     | Shell tooling for recipes                      |
| `libgcc`, `libgomp`, `libstdc++`   | Runtime libs goose dynamically links           |
| `ca-certificates-bundle`, `tzdata` | TLS + timezone data                            |

Goose extensions (`config.yaml`): `developer` (builtin filesystem/shell/editor
scoped to `/workspace`) and `context-forge` (`streamable_http` to the MCP gateway,
for observability/cluster read tools). GitHub access is not a goose extension: it
is the `gh` CLI on PATH, authenticated by the tier's `GITHUB_TOKEN` placeholder
(swapped at egress). Provider and model come from the tier env, not from this file.

---

## 7. Artifacts: Publish and Serve

**Source:** `projects/monolith/artifact/` (`router.py`, `s3.py`, `jobs.py`)

An `/artifact` run's HTML is published by the guest and served live by the
monolith, with the untrusted HTML kept in a sandboxed iframe
([ADR 024](decisions/agents/024-discord-agent-hosted-model-tiers-and-artifacts.md)
decision 4).

- **Publish** (`write_router`, in-cluster only): the guest POSTs the HTML to
  `POST /internal/artifact`; the monolith stores it in S3 and returns the public
  URL `<public-base>/artifact/{id}`. Session-blob routes
  (`/internal/artifact/{id}/session`) back the Model A resume path.
- **Serve** (`read_router`, in-cluster only): `GET /internal/artifact/{id}/raw`
  returns the HTML under a strict CSP; `GET .../version` is the ETag the browser
  poller watches for hot reload.
- **Isolation.** `/internal/*` is never on the public HTTPRoute; the SSR frontend
  is the sole public origin. The raw artifact is framed with
  `sandbox=allow-scripts` (no `allow-same-origin`), so agent-generated HTML has no
  ambient authority even though it runs in the owner's browser.

Because Model B keeps the artifact id stable across a thread's turns, re-running
the same thread republishes to the same id and the open browser tab hot-reloads
via the ETag poll.

---

## Superseded Architecture

For anyone reading old commits or ADRs, these are gone and should not be built on:

| Was                                                          | Status                                                                                                                                                                                                                      |
| ------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `agent-orchestrator` (Go, NATS JetStream job queue)          | Decommissioned. Only a stale UI dir remains under `projects/agent_platform/`. Superseded by fc-invoke.                                                                                                                      |
| `kubernetes-sigs/agent-sandbox` controller + `Sandbox*` CRDs | Rejected in [ADR 022](decisions/agents/022-firecracker-snapshot-restore-controller.md); never deployed. Firecracker-direct chosen instead.                                                                                  |
| Goose sandboxes as long-lived K8s pods / warm pool           | Replaced by disposable Firecracker microVMs restored from a snapshot per invoke.                                                                                                                                            |
| Context Forge as a multi-server federating MCP gateway       | Still deployed (the guest reaches it for developer tools), but flagged for removal in [ADR 020](decisions/agents/020-deprecate-context-forge-mcp-gateway.md): serve the monolith MCP directly, auth at the Cloudflare edge. |
| `submit_job` / `list_jobs` orchestrator MCP tools            | Gone with the orchestrator. Runs are triggered from Discord (and, in future, the `submit()` seam).                                                                                                                          |

---

## Related ADRs

| ADR                                                                                                                           | Status   | Decision                                                     |
| ----------------------------------------------------------------------------------------------------------------------------- | -------- | ------------------------------------------------------------ |
| [019 - Substrate Executor + AgentWorkflow](decisions/agents/019-substrate-executor-agentworkflow.md)                          | Accepted | Opaque `Exec` seam; substrate is goose-agnostic              |
| [020 - Deprecate Context Forge](decisions/agents/020-deprecate-context-forge-mcp-gateway.md)                                  | Accepted | Serve monolith MCP directly, auth at the edge                |
| [022 - Firecracker Snapshot/Restore](decisions/agents/022-firecracker-snapshot-restore-controller.md)                         | Accepted | FC-direct on node-4 (rejects kata-fc / agent-sandbox)        |
| [023 - Egress Secret Proxy](decisions/agents/023-egress-secret-proxy.md)                                                      | Draft    | `kloak:` placeholders swapped at the egress hop              |
| [024 - Discord Agent, Tiers, Artifacts](decisions/agents/024-discord-agent-hosted-model-tiers-and-artifacts.md)               | Draft    | goosecracker: owner gate, tiers, sandboxed live artifacts    |
| [025 - Three-Layer Agent Stack](decisions/agents/025-three-layer-agent-stack-goosecracker.md)                                 | Draft    | Layering: firecracker-substrate / goosecracker / discord     |
| [026 - Fast MicroVM Starts + Stateful Artifacts](decisions/agents/026-fast-microvm-starts-and-stateful-artifact-iteration.md) | Accepted | CoW rootfs, `sessions.db` to S3, disposable VM model         |
| [041 - Hot Git Mirror](decisions/agents/041-hot-git-mirror-agent-workspaces.md)                                               | Draft    | In-cluster mirror decouples repo freshness from VM base      |
| [027 - Agent GitHub App Roles](decisions/agents/027-agent-github-app-roles.md)                                                | Draft    | Implementer / reviewer GitHub apps, scoped permissions       |
| [028 - Elastic MicroVM Capacity](decisions/agents/028-elastic-agent-microvm-capacity-and-reclaim.md)                          | Draft    | State-preserving reclaim of node-4 microVM slots             |
| [030 - fc-invoke](decisions/agents/030-fc-invoke-configurable-firecracker-surface.md)                                         | Draft    | One configurable Firecracker surface (absorbs older daemons) |
| [031 - Control/Data-Plane Split](decisions/agents/031-cluster-node-control-data-plane-split.md)                               | Accepted | Go interface split; enables future cross-node gRPC           |

## Quick Reference

```bash
# Orchestration (in the monolith)
ls projects/monolith/goosecracker/            # dispatch, runner, threads, sessions, tiers
ls projects/monolith/chat/ | grep goose       # Discord gate, bot glue, progress buffer
ls projects/monolith/artifact/                # publish + sandboxed serve
grep -n -A40 '^goosecracker:' projects/monolith/deploy/values.yaml   # tiers, progressUrl, gitMirror

# Execution substrate
ls projects/firecracker/substrate/            # fc-invoke daemon, FC driver, vsock, egress proxy
ls projects/firecracker/goosecracker/guest/   # apko image, goose config, recipes
ls projects/firecracker/goosecracker/guest/recipes/   # agent (router), query, plan, implement, artifact
ls projects/firecracker/git-mirror/           # hot git mirror service
cat projects/firecracker/substrate/deploy/values.yaml # node, egress allowlist, workloads
```
