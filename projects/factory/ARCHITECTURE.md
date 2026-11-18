# Factory architecture

The reusable software factory that runs on the factory home cluster
(`projects/factory-cluster/`). This file records the design for steps 2
and 3 of #7817: installing the foundation and delivering the first reviewed
change. Labels: **[V]** verified in upstream source, docs or live state;
**[I]** inferred, to confirm in the PR that depends on it.

<a id="connector"></a>

See the [connector architecture](connector/ARCHITECTURE.md) for the [run lifecycle](connector/ARCHITECTURE.md#run-lifecycle), [revision rounds](connector/ARCHITECTURE.md#review-rounds) and [liveness lease](connector/ARCHITECTURE.md#liveness-lease). The move keeps the reviewed text without a claim-by-claim source audit ([Joe's decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-265#comment-3955)).

## Components and ownership

| Responsibility | Component | Pin |
| --- | --- | --- |
| Work requests, assignment, work feed, run queue | itsaplan Community | v1.10.0 (`ghcr.io/croffasia/itsaplan-{api,web,worker}`), by digest |
| Agent definitions, execution, sessions | kagent | v1.0.1 charts `oci://ghcr.io/kagent-dev/kagent/helm/{kagent-crds,kagent}` |
| Isolated workspaces (actors) | Agent Substrate, kagent-dev fork | v1.0.1 |
| Coding harness | Codex CLI | 0.169.1, from the `jomcgi-org/kagent` fork build (branch `jomcgi/codex-6.2`, which also carries kagent#3653) |
| Repository, PR, CI, merge queue | GitHub | |
| Provider and GitHub credentials | `projects/factory/broker` | this repo |
| Relationships between itsaplan, kagent and GitHub | [Connector architecture](connector/ARCHITECTURE.md) | this repo |
| Agents' shared long-term memory | Hindsight (`projects/platform-factory/hindsight`) | 1.2.1 (`ghcr.io/vectorize-io/hindsight-api`), by digest |

itsaplan owns the request and its run history, kagent owns execution and
session state, GitHub owns PR, check, review and merge state. The connector
records how they relate and routes results and feedback between them.

## Findings that shape the design

1. **kagent builds against the kagent-dev Substrate fork.** [V] kagent
   v1.0.1 `go.mod` replaces `go.ate.dev/substrate` with
   `github.com/kagent-dev/substrate v1.0.0`, a fork 82 commits ahead of
   upstream that adds Helm charts, the Kubernetes-Secret credential provider
   and the egress gateway kagent uses. kagent needs ate-api-server preview
   gates `PerTemplateSnapshotLocation` and `GoldenEgressPolicy`, the `k8s.io`
   credential provider with namespace policies, the egress MITM CA, the
   kagent controller in the router SPIFFE allowlist and bootstrap owners, and
   `ate.dev/api-access=true` on the kagent namespace. The first Substrate
   install (#7829) was upstream v1.0.0; #7837 moved it to the fork's 1.0.1
   charts with these settings. The provider's grant covers only Secrets in
   `kagent` labelled `substrate.ate.dev/injectable=true`, and it reads only
   namespaces holding a RoleBinding for it, which the kagent install adds. An
   admission policy reserves the `kagent-controller` identity for ArgoCD.
2. **The Codex harness speaks the OpenAI Responses API.** [V] Its OpenAI
   provider accepts only `baseUrl` and `apiFormat: responses`, authenticated
   by an API key or an `actorJwt` that Substrate mints per actor and injects
   at the egress gateway. The Substrate gateway replaces a header the client
   sends and never adds one, while the ChatGPT backend also expects
   `chatgpt-account-id`. The broker therefore relays model calls (below).
3. **OpenAI advises against ChatGPT-account auth for public repositories**
   and against sharing one `auth.json` across streams. [V] Using the
   subscription for `jomcgi-org/homelab` is Joe's decision.
4. **The itsaplan chart renders its secrets from values.** [V] That
   conflicts with 1Password delivery, so the factory chart carries its own
   itsaplan templates against the upstream images.
5. **kagent's trusted-proxy mode decodes bearer JWTs without verifying
   them.** [V] Network policy must leave the edge, the kagent UI, the
   connector, the reviewer when enabled, Substrate's `atenet-egress`
   (actors' calls, which carry a verified actor JWT) and the controller itself (its own `kagent-api`
   RemoteMCPServer) as the only bearer paths into the controller.
6. **A kagent service-account principal includes the ServiceAccount UID.**
   [V] Recreating the connector's ServiceAccount changes its identity and
   loses access to its sessions, so that ServiceAccount is never pruned and a
   restore includes an ownership transfer.
7. **itsaplan run semantics.** [V] An external run's lease defaults to 300 s
   and fails after 3 handouts without a result. A reported failure is never
   retried. Heartbeats and results check `attempt` only when it is sent, so
   the connector always sends it. Once a run leaves `pending`, a heartbeat
   gets 404, the same answer as a reclaimed attempt. [I] At v1.10.0 the only
   path that cancels a started run is moving its issue to another project;
   the itsaplan PR confirms the operator's cancel action.
8. **kagent speaks A2A v1.0.** [V] The gateway at
   `/agents/{namespace}/{name}` takes JSON-RPC methods `SendMessage`,
   `GetTask`, `ListTasks` and `CancelTask`, requires the `A2A-Version: 1.0`
   header, and uses `TASK_STATE_*` and `ROLE_USER` values. `SessionService`
   is gRPC only, served as h2c on the controller's `:8083` listener.
9. **kagent accepts a task-opening message once per session.** [V] A
   message without a task ID opens a task. `ReserveSessionDispatch` refuses
   one whose message ID the session already holds, and the gateway then
   answers with the task holding it. A first send with no context ID also
   finds its session again: the session's request ID derives from the Agent
   and message ID. A reservation lasts two minutes and blocks other
   dispatches to the session (`KAGENT_SEND_NOT_ACCEPTED`), and kagent rejects
   a first save that arrives after it. A failed turn carries status text
   only; the Codex error category stays in kagent's traces.
10. **A session belongs to one kagent Agent.** [V] Every A2A call checks
    that the session belongs to the Agent in the URL.
11. **itsaplan has no per-run read for an agent key.** [V] The runner queue
    (`GET /agent-runs/queue?runId=`) needs `work_items read` and
    `ai_agents read` in the run's project, and it hides runs whose issue
    moved to another project.
12. **kagent reports usage per model call.** [V] Its ADK runtimes put each
    call's usage in metadata key `kagent.dev/a2a/usage` (`promptTokenCount`,
    `candidatesTokenCount`) and a task total under the usage extension
    `https://kagent.dev/extensions/usage/v1` (`inputTokens`,
    `cachedInputTokens`, `outputTokens`, `reasoningTokens`). At v1.0.1 the
    Codex and Claude Code harnesses put neither on the task, though their
    Agent Card lists the extension: each call's usage, cache reads included,
    goes only to OpenTelemetry `chat` spans (`gen_ai.conversation.id` is the
    context ID) and `gen_ai.client.inference.usage.*` counters
    (`go/harness/runtime/a2a`).
13. **An agent's comment starts no itsaplan run.** [V] At v1.10.0
    `queueMentionRuns` returns early when the comment's author is an agent
    user, so the coordinator cannot start a mention-run. A delegation run
    starts when the issue's delegate changes, with a fixed prompt.
    `enqueueDelegateRun` skips self-delegation; `isTriggerableBy` accepts
    another agent when the target has `runnerScope: team`. The bootstrap
    gives `homelab-codex` that scope and `triggerOnAssign: true`, so
    `factory-coordinator` can start it (HOME-118, also applies to HOME-106).
    Core v1.10.0 includes cron and status schedules without Pro. Its
    `agent_schedule` schema and CE `ScheduleDialog.tsx` expose them.
    Status delays and delegation delays stop at 86400 seconds; cron runs
    carry no issue. Sources:
    [delegation](https://github.com/croffasia/itsaplan/blob/v1.10.0/apps/api/src/modules/issues/service.ts#L1395),
    [trigger scope](https://github.com/croffasia/itsaplan/blob/v1.10.0/apps/api/src/modules/agents/core/service.ts#L399),
    [schedule schema](https://github.com/croffasia/itsaplan/blob/v1.10.0/packages/db/src/schema/app.ts#L668),
    [CE dialog](https://github.com/croffasia/itsaplan/blob/v1.10.0/apps/web/src/ce/ScheduleDialog.tsx).
14. **kagent session recovery.** [V] `RevertSession` restores a crashed
    runtime to its last durable snapshot without replaying the interrupted
    turn, returns a session that is not crashed unchanged, and answers
    `FAILED_PRECONDITION` when the runtime cannot be recovered (Session or
    Actor deleted, Actor replaced or retemplated): the client must replace
    the session. Idle expiration deletes a session, so later calls answer
    `NOT_FOUND`. [I] A crashed runtime leaves its task working until the
    revert fails it.
15. **kagent authorizes no Agent per caller.** [V] The v1.0.1 controller
    builds its authorizer from `app.Options.Authorizer`, which its `main`
    leaves nil, so users and ServiceAccounts get `NoopAuthorizer`: any
    authenticated principal may invoke any Agent the controller watches.
    Only actors are confined, to their own session. Session ownership still
    separates principals. ServiceAccount tokens are accepted only once
    `config.server.auth.serviceAccount.authority` is set; kagent then reviews
    them for the fixed audience `kagent-invocation`, and the principal ID is
    `kubernetes:<authority>` with the ServiceAccount UID.

## Umbrella chart

`projects/factory/chart` is a published `helm_chart` (OCI
`ghcr.io/jomcgi/homelab/charts/factory`). The broker and connector images
are in its `images` attr, pinned at build time.
`projects/factory/deploy` holds the homelab
Application and values. It installs:

- kagent (subchart, `kagent.mode: install|existing`).
- itsaplan (own templates), the broker, the connector.
- A secret-init Job that creates rotatable keys only when absent.
- A CNPG `Cluster` (`postgres.mode: install`; `existing` takes Secret
  references only).
- kagent CRs per repository binding: ModelConfig, Harness, Agent,
  EgressPolicy, and the binding's namespace.
- HTTPRoutes and Cilium policies.

Substrate, Authentik, the Envoy edge, the OTel collectors and Garage stay
separate installations that the chart references.

**Bundled values migration (#7909), preparation.** The chart also packages
`chart/values-homelab.yaml` as an optional file, without merging it into
`values.yaml`. For now the Application still reads `deploy/values.yaml`
from git. Keep the two files identical; `package_test.py` checks the actual
archive against the git values and renders both with CI's image pins. The
source render remains covered by `deploy/render.sha256`. The Application,
git values and chart defaults are unchanged in this preparation release.

**Why stage the switch.** The currently pinned chart predates the bundled
file. Switching before publication breaks manifest generation, and removing
the git source also breaks rollback to that old chart. First publish this
compatible chart through CI and wait for its pin write-back. The follow-up
cutover changes the Application to `valueFiles: [values-homelab.yaml]` and
removes the git values source only once the previous release available for
rollback also contains the file. Verify a render of both published archives
with their own bundled values before landing that switch. Keep missing
values an error. Do not enable new chart fields in the git values during
preparation; the current split-PR rule still applies.

The cutover also removes the duplicate `deploy/values.yaml`, points local
render targets at the bundled file, updates the Application contract test,
and retires the split-PR guidance through the required review. After that,
chart and homelab values changes ship in one PR; lowering the pin restores
both from the selected release. Before cutover, lowering the pin continues
to use the existing git values, including for the pre-bundle release.

**Repository bindings.** [V] kagent runs an Agent's actors in the atespace
named after its namespace, so the binding namespace is also the atespace that
Substrate's credential provider grants. A Harness needs a Substrate
WorkerPool in its own namespace, so each binding has one. Its replicas
are the agent's concurrency, with one actor per worker. kagent 1.0.1 sets
one CPU and memory limit for every runtime (`config.runtimeDefaults`), so
each agent's resource limits must equal it, and the chart refuses others. A
worker's capacity is its pod memory limit less a 64Mi reserve, and each actor
books its limit plus the gVisor sentry, so worker pods get 1Gi more memory
than their actor.
Substrate's egress gateway admits actors to the broker's relay and to
`github.com`. Its credential provider reads a binding namespace only through
the chart's RoleBinding and the namespace's `namespacePolicies` entry.

**Ordering.** kagent CRDs are vendored in `crds/`; a PreSync/pre-upgrade
Job server-side-applies them so upgrades update CRDs. Waves: namespaces,
ServiceAccounts, 1Password items and the database (-2); secret-init and a
database-ready gate (-1); kagent, itsaplan api, broker (0); itsaplan web and
worker, connector (1); kagent CRs and the itsaplan bootstrap Job (2).

**State kept across upgrades.** itsaplan `APP_ENCRYPTION_KEY` and
`BETTER_AUTH_SECRET` are generated once in the cluster and never change
("itsaplan" below). Generated keys,
the Codex grant and GitHub tokens are ignored by ArgoCD diffs. The CNPG
Cluster, PVCs and the kagent and connector ServiceAccounts carry
`Prune=false,Delete=false` and `helm.sh/resource-policy: keep`. Acceptance:
two resyncs leave Secret `resourceVersion`s and database row counts unchanged.

**Configuration** covers repositories (owner, repo, base branch, itsaplan
project, agent, namespace, branch prefix, concurrency, required and trusted
checks, merge policy, retention), agents (harness, provider, resources,
concurrency, egress grants, permitted tools), providers, and each external
service in install or existing mode. A repository binding is the access
boundary: one Agent and namespace per repository, with credentials scoped to
it.

## Sandbox checks

`repositories[].toolchain: homelab` gives one coding binding the bootstrap
instructions in `chart/files/toolchain-prompt.txt` and the `homelab-tools`
EgressPolicy. Review bindings do not inherit it. The Substrate worker image
keeps its existing digest pin.

**Why.** HOME-2 could run no local checks in the Alpine Codex harness
(#7916). The lighter toolchain option supplies local formatting and chart
checks without introducing a BuildBuddy credential. The tools OCI image
contains GNU libc runtimes; the harness uses musl. Installing it unchanged
would leave Node, Python and Ruff unable to execute. kagent v1.0.1's Codex
compiler also omits `Harness.workload.command` and `args` from its runtime
revision, so a command override cannot run setup before the adapter starts.
The binding's system prompt requires bootstrap immediately after cloning.

From the checkout, run `bash projects/factory/toolchain/bootstrap.sh`, then
source `${XDG_CACHE_HOME:-$HOME/.cache}/homelab-sandbox-tools/env.sh` in each
shell. Setup uses the pinned harness's Bash, APK, system utilities and Alpine
signing keys; it runs as the sandbox user. APK installs
Node 22, Python, pytest, PyYAML and GNU grep in a private root from the signed
Alpine 3.22 repositories. Those packages take that stable branch's security
updates on a fresh cache. Each runtime wrapper sets its own library path;
the harness and git retain their libraries and CA configuration.

Helm, crane, buildifier, gofumpt, shfmt and shellcheck use the Linux amd64
URLs and SHA-256 hashes in `bazel/tools/tools.lock.json`. Ruff's musl wheel
uses that lock's version. pnpm, prettier, the Svelte plugin and Svelte use
`package.json` versions, installed without lifecycle scripts. PyPI and npm
verify their distribution integrity; their transitive dependency selection
is resolved at bootstrap, outside the Bazel dependency graph. Setup checks
the executables before publishing an environment file and caches them by
the bootstrap, installer and repository version inputs. A missing executable
repairs the cache; a failed download never publishes a new environment.

The download allowlist is `dl-cdn.alpinelinux.org`, `get.helm.sh`,
`release-assets.githubusercontent.com`, `registry.npmjs.org`, `pypi.org` and
`files.pythonhosted.org`. GitHub release requests use the binding's existing
`github-git` origin. The new origins have no credential injection, and no
BuildBuddy origin or key is configured.

Agents can run `ci lint`, `ci regen`, `ci ratchet`, `helm template` and scoped
`python3 -m pytest` checks. PR CI owns BUILD generation and remote tests;
these local checks do not establish a passing `ci test` or full `ci` run.

**Rollout.** The chart capability is staged. Per `REVIEW.md`, add
`toolchain: homelab` to the homelab repository entry in `deploy/values.yaml`
only after CI publishes and pins the supporting chart. That follow-up must
update `deploy/render.sha256`, start a fresh coding session, run bootstrap
and local checks, and confirm its review binding has no download grant.
An existing session with the old egress policy cannot validate downloads.

**BuildBuddy migration blocked (HOME-133).** The proposed gateway replaces
the gRPC authentication header, but the pinned CLI also copies its stored
login into the remote Bazel command. In CLI 5.0.321,
[`ConfigureAPIKey`](https://github.com/buildbuddy-io/buildbuddy/blob/7cd8e347188466582fe66fd24ac6df12568fc9ed/cli/login/login.go#L325-L348)
appends `--remote_header=x-buildbuddy-api-key` from `.git/config`.
[`parseArgs` and `HandleRemoteBazel`](https://github.com/buildbuddy-io/buildbuddy/blob/7cd8e347188466582fe66fd24ac6df12568fc9ed/cli/remotebazel/remotebazel.go#L1170-L1277)
put those arguments into the command sent in `RunRequest`.
A stored placeholder therefore reaches the runner's Bazel arguments; gateway
header replacement does not rewrite that request body.
[HOME-133](https://factory-plan.jomcgi.dev/factory/issue/HOME-133) requires
stopping and reporting this condition. The BuildBuddy policy, toolchain
additions and remote-test prompt are not enabled or implemented.
Resolve placeholder transport before staging them. Live gRPC, runner
authentication, warm-cache reuse and memory acceptance remain unverified.

## Memory

`repositories[].memory` binds one coding binding's Agent to Hindsight
(`projects/platform-factory/hindsight`) as RemoteMCPServer `memory`, limited
to `recall` and `retain`, and adds `chart/files/memory-prompt.txt` to its
prompt: recall before planning, retain lessons with their issue, PR or commit
and whether CI confirmed them. Review bindings do not inherit it. The
`homelab` coding binding uses bank `homelab`
([HOME-185](https://factory-plan.jomcgi.dev/factory/issue/HOME-185)).

**Why.** Joe made memory high priority on 2026-11-16 to cut tokens per
delivered issue, with Claude Haiku as Hindsight's LLM so that memory writes
do not spend Codex quota
([HOME-130 decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-130#comment-913)).
Agents rediscover the same failing checks and conventions on each run; a
recalled lesson costs a few hundred tokens.

Hindsight runs in namespace `hindsight`, one replica of the upstream image,
with no local embedding or reranker model. Its state is database `factory_hindsight` on
`factory-db`, so the existing WAL archive and daily backups cover it.
`retain`, consolidation and `reflect` call `claude-haiku-5-5` with the key
from 1Password `factory-hindsight`. Embeddings use `mongodb/voyage-4-large`
through OpenRouter, with the key from 1Password `factory-discord-llm` that
Discord memory also uses, and OpenRouter routes only to providers that keep
no data. The decision model, `typesafe/jev-1.14` through OpenRouter's System
One endpoint on the same key, ranks recall and settles the yes-or-no
judgments of consolidation, reflect and page refresh. The reranker is `rrf`,
which loads no model. Recall has no local fallback, so it fails while
OpenRouter or Jev is unavailable. Hindsight cannot send OpenRouter's data
policy with decision calls, so the preflight requires Jev to have exactly
one OpenRouter endpoint, and requires that endpoint to answer a request that
requires zero data retention. OpenRouter's routing preferences cannot pin
one variant of a provider, so a second endpoint stops the sync for a person
to check.

**Why the decision model.** Joe chose a decision model with no local
fallback ([his words, recorded from his account on #8147](https://github.com/jomcgi-org/homelab/pull/8147#issuecomment-6949878652);
also HOME-130 comment 1498).
Hindsight's own question decided the order of the top 30 Voyage 4 Large
candidates for the same 32 graded questions. On candidates from
embeddings and BM25 together, Jev 1.14 raised nDCG@10 by 0.079, in 0.29 s per
recall ([HOME-182](https://factory-plan.jomcgi.dev/factory/issue/HOME-182)).
Liquid `d1` made no significant difference; Cloudflare `clef` and Upstage
`solar-decide-flash` made it worse.

**Why.** On a 494-fact factory test set, Voyage 4 Large had the best
nDCG@10 (0.781) of ten models, against 0.522 for a stand-in for the image's
local `bge-small-en-v1.5`. Its 1,024 dimensions fit pgvector's index limit
([HOME-176](https://factory-plan.jomcgi.dev/factory/issue/HOME-176)). Joe
chose one OpenRouter key for both instances
([HOME-130 comment 1439](https://factory-plan.jomcgi.dev/factory/issue/HOME-130#comment-1439)).

**Preflight.** Hindsight resizes an empty vector column at startup and
refuses to start on a model with another dimension once rows exist. Each sync
first runs the `hindsight-preflight` Sync hook (wave -1, after the 1Password
items). It needs the same Secrets as the API pod, embeds one string through
OpenRouter, asks the decision model one question, and fails when the key is
empty or refused, when the embedding model returns another dimension, when
the decision model gives no answer, or when stored rows in any schema have
another dimension. The sync then stops before the Deployment, and the running pod
keeps serving.

**Rollback.** Two levers, by what failed:

- **The decision model.** Revert the commits of #8147 (HOME-182). Recall
  returns to the image's `hs-rerank-1` cross-encoder, and consolidation,
  reflect and page refresh to the LLM. Nothing stored changes.
- **OpenRouter as a whole.** Revert #8147's commits first, then #8144's
  (HOME-176). Together they remove every OpenRouter setting, the preflight
  and their tests, so nothing references the OpenRouter Secret. While no rows
  hold vectors, the reverts are enough. After the first retain, re-embed
  first; no API pod has to run:

  1. `kubectl apply -f projects/platform-factory/hindsight/recovery/reembed-local.yaml`
     re-embeds every row with the local model. ArgoCD never applies this Job.
  2. Merge the reverts.
  3. In the running pod, run `hindsight-admin reembed --all-schemas --finish`.

`projects/platform-factory/hindsight/rollback_test.py` runs these steps in the
deployed image without network, from 1,024-dimensional vectors, and
`keys_test` pins the recovery Job to its settings.

**Schema v1.** Every new bank starts from
`projects/platform-factory/hindsight/resources/bank-template.json`, which the
Deployment passes as `HINDSIGHT_API_DEFAULT_BANK_TEMPLATE`
([HOME-183](https://factory-plan.jomcgi.dev/factory/issue/HOME-183)). Its
retain mission keeps reusable lessons, incidents, decisions and pointers with
their issue, PR or commit, and drops chatter, logs and credentials. Two label
groups have `tag: true`, so extraction tags each fact. `app:<path>` names the
one project the fact is mainly about: a directory under `projects/`,
`projects/platform/`, `projects/platform-gke/` or `projects/platform-factory/`,
or one of those three groups' own roots. `kind:` is `lesson`, `incident`,
`decision` or `reference`. Consolidation builds observations per `app:*`
tag. Hindsight puts every tag a scope glob matches into one scope, so `app`
takes one value: a fact about two projects would otherwise reach neither
project's observations. A fact with no `app` tag consolidates over its own
tags. Recall can filter on both tags. Hindsight
applies the template only when it creates a bank. So the
`hindsight-bank-template` PostSync Job runs after every sync: it creates bank
`homelab` when it is missing, imports the template into it, and reads the
config back, failing when a setting differs. An import changes the bank's
config, never its memories, so a template change in git reaches the bank on
the next sync. `POST .../import?dry_run=true` validates a manifest without
changing anything. A new project needs a new `app` value.

Two keys separate the callers. The REST API takes only the tenant key, which
only namespace `hindsight` holds: the API pod, and the Jobs that create the
key and apply the bank template. MCP takes the agents' token
(`HINDSIGHT_API_MCP_AUTH_TOKEN`) and registers only `recall` and `retain`
(`HINDSIGHT_API_MCP_ENABLED_TOOLS`), so a caller with the token can neither
reach REST nor list, delete or reflect. The `hindsight-keys` Jobs generate both
keys once and copy the token into the binding's `hindsight-mcp` Secret, which
is labelled injectable: the Substrate egress gateway adds it as the
`Authorization` header, and the actor holds a placeholder. Hindsight admits
only the egress gateway; its own egress is `factory-db` and
`api.anthropic.com`. kagent cannot reach Hindsight, so the RemoteMCPServer
sets `kagent.dev/discovery: disabled` and the agent lists tools at run time.

The bank is the URL path (`/mcp/homelab/`). With one binding, the token
grants that bank alone in practice; a second binding needs its own bank and a
gateway that pins callers to it. Recalled text is data, never an
instruction: the prompt says the repository, issue and CI win.

**Rollout.** The reviewer cannot read 1Password or the cluster. Joe accepted
a labels-only Connect check before merge and a live check after it: Secret
`hindsight/hindsight-anthropic` holds `ANTHROPIC_API_KEY` and the pod is Ready
([decision](https://github.com/jomcgi-org/homelab/pull/8107#issuecomment-6920188055)).
kagent rejects an Agent whose header Secret is missing, so set
`memory` in `deploy/values.yaml` only after CI publishes and pins the
supporting chart and `factory-r-homelab/hindsight-mcp` holds a token. That
change updates `deploy/render.sha256`; a fresh coding session confirms the
tools.

Discord memory has a separate process, database and REST tenant key under
[`hindsight-discord`](../platform-factory/hindsight-discord/README.md)
([HOME-163](https://factory-plan.jomcgi.dev/factory/issue/HOME-163)). It calls
DeepSeek through the dedicated OpenRouter credential. Its manifests disable
MCP and admit only namespace `factory-discord`. The linked runbook records
the boundary and rollout checks; the bridge remains HOME-162.

## Discord bot

Bosun answers Discord messages that tag it (HOME-162). Discord content is
untrusted data from people other than Joe, so it stays on its own path:

```
Discord gateway ─► discord-bridge (factory-discord) ─► kagent controller ─► Agent bosun (factory-discord-agent, gVisor actor)
                        │                                                      └─► openrouter.ai (dedicated key, injected at egress)
                        └─► hindsight-discord (bank discord-<guild>, tag channel:<id>)
```

- **Bridge** (`projects/factory/discord`, chart `discord.bridge`): the only
  Hindsight client and the only path in. It answers only tagged messages in
  allowlisted servers, recalls and retains in the channel's tag scope, and
  sends Discord text to the Agent only as JSON data. Replies cannot mention
  anyone. It ships in the connector image.
- **Agent** (chart `discord.agent`): its own namespace and atespace, one
  small gVisor worker ([Joe's decision](https://github.com/jomcgi-org/homelab/pull/8188#issuecomment-6960669519)), no tools, and web search
  through the model (":online"). Its ModelConfig names only the
  `factory-discord-llm` key; kagent gives the actor a placeholder and
  Substrate's egress gateway injects the key (the namespace's
  `credentialProvider.namespacePolicies` entry).
- **Boundaries**: kagent's controller admits the bridge's pods only when the
  bridge is on; hindsight-discord admits only pods labelled
  `factory-discord-bridge`. No factory agent, key or memory sees Discord
  content.
- **Rollout**: the chart ships both off. A values change turns the Agent on
  once a chart version with these templates is published (the deploy pins a
  published chart), and its namespace joins `kagent.rbac.namespaces` then.
  The bridge stays off until Bosun's token is in 1Password item
  `factory-discord` (field `token`) and the hub monolith's bot is off, so no
  message gets two replies. Substrate's egress gateway admits `openrouter.ai`
  for the Agent's model calls.

## itsaplan

Step 6 (#7817). The chart's own templates (`templates/itsaplan*.yaml`) run
the upstream images `ghcr.io/croffasia/itsaplan-{api,web,worker}:1.10.0` by
digest in namespace `factory`; `itsaplan.enabled` switches them on.

- **Origin.** `https://factory-plan.jomcgi.dev`, behind the edge's
  gateway-wide Authentik sign-in. The web app is served at `/` and the api at
  `/backend` on the same host: the HTTPRoute removes the prefix with a
  `URLRewrite`, and `API_URL` carries the path. [V] v1.10.0 supports this
  path mode (`packages/auth/src/index.ts` gives better-auth the origin and
  puts the path back into links, OAuth redirect URIs and discovery), although
  the upstream chart's values comment still says it does not; the trial's
  OIDC sign-in built `https://factory-plan.jomcgi.dev/backend/api/auth/oauth2/callback/oidc`
  and Authentik accepted it. The session cookie is host-only (`__Host-`).
- **Data.** PostgreSQL is the CNPG Cluster `factory-db` in its own namespace
  (`projects/platform-factory/factory-db`): three instances, WAL and daily
  base backups to Garage bucket `factory-db-backups`. Its roles and databases
  are declared (managed roles, a `Database` object), so it has no superuser
  login; a setup Job writes `DATABASE_URL` Secrets `factory-itsaplan-db` and
  `factory-connector-db` into `factory`. Attachments use Garage bucket
  `factory-itsaplan` (`factory-itsaplan-s3`).
- **Migrations** run in the api pod's init container, as upstream's chart
  does, under itsaplan's advisory lock; the worker waits for them. The
  pre-migration `pg_dump` is skipped (`SKIP_PRE_MIGRATION_BACKUP=1`): the dump
  would live in an `emptyDir`, and WAL archiving already restores to any point
  before a migration.
- **Secrets.** `secret-init` (wave -1) generates `BETTER_AUTH_SECRET`,
  `APP_ENCRYPTION_KEY` and the break-glass admin's password once, into
  Secrets the chart declares empty and keeps. They move to 1Password
  (`k8s-homelab/factory-itsaplan`) once Postgres has off-site backups, as for
  Authentik's key: until then a lost cluster loses the database they protect.
- **Human sign-in.** itsaplan's own OIDC client `factory-itsaplan` in
  Authentik (blueprint `factory-blueprints/factory-itsaplan.yaml`), bound to
  `authentik Admins`. Authentik's secret-init generates its client secret and
  copies it into `factory/factory-itsaplan-oidc`, which `factory-buckets`
  declares so it exists before the chart enables itsaplan. Registration is
  invite-only, so a person is invited by an admin before their first OIDC
  sign-in. The break-glass admin (`factory-itsaplan-admin`) signs in with its
  generated password, still behind the edge's sign-in, for bootstrap and
  recovery only.
- **Joe's sessions.** Claude and Codex sessions on Joe's account use
  `joe-sessions@jomcgi.dev`, displayed as "Joe (sessions)". itsaplan 1.10.0's
  human username validator excludes hyphens, so its handle is `@joesessions`.
  The bootstrap invites it into the bound project on the default member role,
  which permits issue edits and comments, and checks that it is neither an
  agent nor an admin of the factory workspace. Before signing it up, bootstrap
  generates and saves its password in `factory/factory-itsaplan-joe-sessions`
  (`email`, `password`). Reruns reuse the account and password, finish a pending
  invitation, and refuse to replace a lost password for an existing account.
  The chart keeps the Secret and ArgoCD ignores its data. Only the bootstrap
  Job has in-cluster access through its existing RoleBinding; the Secret has
  no sandbox injection label and its namespace has no credential-provider
  grant. Joe's sessions read it with the factory kubeconfig
  (`docs/agents/reference.md`, "Tracker"). This separates Joe's recorded
  decisions from bootstrap activity ([HOME-59](https://factory-plan.jomcgi.dev/factory/issue/HOME-59),
  [Joe's decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-57#comment-292)).
- **Bootstrap** (PostSync, `files/itsaplan/bootstrap.py`) signs in as that
  admin, creating it on an empty database (the first account is itsaplan's
  instance admin); sets invite-only registration and the OIDC provider; and
  creates team `factory`, project `HOME` and the external agents
  `homelab-codex` (claims runs, assignment and mention triggers) and
  `factory-coordinator` (no triggers; comments, issue state and run lookups
  on the default role; the trial confirmed `work_items` and `ai_agents` read
through the runner queue, and the connector step confirms its writes). Each
  agent's once-returned key goes to `factory-itsaplan-agent-<username>`. A
  Secret without a working key gets that agent a rotated key; an agent is
  never created twice. It signs out at the end.
  With the reviewer enabled, bootstrap also creates `factory-reviewer`,
  without triggers, and a dedicated role granting only `work_items read`.
  It starts with no projects, then receives that role in the bound project;
  reruns repair permission drift and remove other project memberships before
  writing its key. The reviewer service mounts only that key, with an
  optional Secret projection so the Deployment can become healthy before
  PostSync bootstrap. Missing keys fail issue reads closed.
- **Key safety.** secret-init counts the database's accounts, settings and
  stored secrets (read-only, the api image's psql) and refuses to replace a
  lost `BETTER_AUTH_SECRET` or `APP_ENCRYPTION_KEY` for a database that has
  any: new keys would end every session and make the stored credentials
  unreadable. An annotation on the Secret accepts the loss; secret-init then
  deletes the `app_secret` rows encrypted with the lost key, since itsaplan
  decrypts the stored OIDC settings before it accepts new ones, and restarts
  the api and worker so the bootstrap writes through pods holding the new
  keys (factory-cluster README, "itsaplan").
- **Public api surface.** The route answers 404 for MCP and for the runner
  feeds (`claim`, `heartbeat`, `events`, `result` of runs and chats), so
  claiming and reporting runs happens only inside the cluster. An agent key
  still authenticates on the api's other routes through the public origin,
  for anyone past the edge's sign-in (`authentik Admins`). God mode stays,
  since the web app calls it for the instance admin.
- **Bootstrap coupling.** The bootstrap stays a PostSync hook of the factory
  Application: a run that cannot sign in or write agent keys leaves the
  factory unusable, and a failed sync is where that should show. Its waits
  are bounded (30 minutes), and it writes only what differs. The OIDC
  settings carry a client secret itsaplan never returns, so Secret
  `factory-itsaplan-bootstrap` records a digest of what was last written and
  when; they are rewritten when that digest or a visible field changes, and
  on the first sync more than a day after the last write, which also undoes
  a client secret changed in god mode.
- **Pods** run as UID 65532 with read-only root filesystems and no
  ServiceAccount token; the web app gets no Secret. Upstream's images run as
  root by default.
- **Network.** The edge reaches web (3001) and api (3000); web, worker,
  bootstrap and the connector reach the api; the api and worker reach
  `factory-db` and Garage; the api reaches the OIDC issuer by its public
  name. The Telegram bot is not deployed, and outbound mail, webhooks and
  link previews have no egress.

Deviations found in the trial: an empty claim answers `200 {"run":null}`,
not 204; `/me` answers 200 with `authenticated: false` for a refused key, so
a key check reads that field. MCP and its OAuth discovery
(`/.well-known/*`) are not public: the edge's sign-in stands in front of
them, and agents use keys inside the cluster.
CloudNativePG waits `smartShutdownTimeout` for clients before a deleted
primary stops, and the api holds its connections, so `factory-db` sets 15 s
(the default 180 s delayed the trial failover by three minutes).

## Credential broker

`projects/factory/broker`, one replica with `Recreate`. It reuses the Codex
OAuth code in `projects/shared/go/tokenbroker`, with the grant Secret prefix
(`factory-broker-grant-`), metric prefix (`factory_broker_`) and refresh
window configured per consumer; EmberVM keeps its defaults. No Lease: grant
writes are newer-only and a `refresh_token_reused` answer rereads the Secret
and retries once, using a newer durable grant when available. Repeated reuse
stops further provider calls for that token and exposes `refresh_blocked` in
the grant state metric and admin responses. Valid cached access tokens remain
usable. The operator can await a pending writer or complete a new device login
if the replacement was lost. Storage reads cannot distinguish a lost rotation
from a successful rotation whose writes keep failing, so elapsed time never
promotes this state to `login_required`. Relay failures remain recoverable 503s
without causing the connector to persist a credential block. A newer stored
grant with a different refresh token or a completed device login clears the
reuse state, even after a prolonged persistence failure.

- **Codex relay.** The ModelConfig uses `provider: OpenAI`,
  `openAI.apiFormat: responses`, `openAI.baseUrl` set to the broker's
  Service with `/v1` (a DNS name, not an IP), and `actorJwt.audiences:
  [factory-broker]`. [V] The name carries the cluster domain: Substrate
  resolves actor destinations as absolute names, so `<svc>.<ns>.svc` gets
  NXDOMAIN. [V] kagent binds that origin's `authorization` header
  with prefix `Bearer `; Codex in the actor holds a placeholder that the
  Substrate gateway replaces with a JWT minted for the actor, also over
  cleartext `http`. The broker verifies it against the issuer's discovery
  document and key set: signature (ES256 or RS256), `iss`, `aud`, `exp` and
  `nbf` with 30 s skew, `sub` = `actor/<atespace>/<name>` with the
  `actor.ate.dev` claim, and it rejects `worker-for-actor` tokens. Keys
  refresh every 5 minutes; an unknown `kid` refetches at most every
  30 seconds. The call is logged against the actor, atespace and actor UID
  (the UID can be stale for a recreated actor, Substrate #2208). The relay
  accepts `POST /v1/responses`, `POST /v1/responses/compact` and
  `GET /v1/models` (the last two [I]), and forwards to
  `chatgpt.com/backend-api/codex` with the grant's access token and
  `chatgpt-account-id`, streaming SSE back. That the backend accepts the
  Responses request Codex sends in API-key mode is [I] until the first live
  run. Provider credentials exist only in the broker.
- **Relay errors** are JSON `{"factory_error": ...}`: `actor_unauthenticated`
  (401); `codex_login_required` (401, for an empty grant or `invalid_grant`);
  `codex_quota_exhausted` (429 with `resets_at`, from the `x-codex-*` headers or a
  `usage_limit_reached` body; a current exhausted window blocks before the
  backend is called). It sends no `Retry-After`: Codex sleeps until that
  deadline, so on 2026-11-16 three turns held coding slots for 12 hours.
  Without it, Codex 0.161.0 fails the turn at once, and the connector pauses
  the grant until `GET /codex/quota` reads usable
  (HOME-127); `codex_grant_unavailable` (503, transient: a failed
  refresh, an unresolved consumed token, a missing grant Secret, or the
  backend refusing a freshly refreshed token); `relay_busy` (503). A backend 401 replaces the token
  once, and concurrent 401s for the same token wait on that one refresh.
  Other backend 429s pass through for Codex to retry. The connector treats
  the login and quota codes as credential and quota failures.
- **Limits.** Request bodies up to 32 MiB read within 2 minutes, at most 64
  requests in flight, and any upstream response silent for 5 minutes is cut.
  The body watchdog starts at response headers for each attempt. Initial
  and retried 401 drains and 429 inspection each read at most 64 KiB within
  `StreamIdleTimeout` (5 minutes by default), even if bytes keep arriving.
  A timed-out 401 drain still permits the one token refresh and retry;
  two stalled 401 bodies spend at most twice that limit reading bodies,
  plus the separately bounded refresh and response-header waits. A stalled
  429 returns `codex_upstream_unreachable` (502) unless quota exhaustion is
  already known. Other responses reset the idle deadline as bytes arrive,
  so active SSE has no overall time limit. Actor cancellation aborts body
  reads immediately; completion or failure releases the concurrency slot.
  Quota observations are committed off the request path, latest only.
- **Quota reading.** A reading comes from relayed `x-codex-*` headers or
  from `GET chatgpt.com/backend-api/wham/usage`. It expires 30 minutes after
  it arrives, or at its first window reset if that is sooner. Every minute
  the broker checks the stored reading and reads the usage endpoint once it
  has expired, so relayed traffic defers the read. A failed read keeps the
  old reading and retries after 5 minutes; one read runs at a time.
- **Refresh.** One grant, `codex-primary`, from the broker's own device
  login: `POST /admin/codex/login` returns the user code, `GET` reports
  state, `POST /admin/codex/refresh` forces a refresh. These require a
  projected ServiceAccount token (audience `factory-broker`) whose TokenReview
  username is in `tokenReview.adminSubjects`. `broker.operatorServiceAccount`
  creates a ServiceAccount for operators and adds it there; they mint a
  short-lived token for it, and a ValidatingAdmissionPolicy keeps any Pod
  from running as it. The grant refreshes when under
  24 h from expiry or 7 days old, single-flight, written to the Secret
  before use, checked every 10 minutes without traffic. When a refresh fails
  but the stored access token is still valid, the relay keeps serving it and
  the grant state metric reports the failure. The chart pre-creates the
  empty grant Secret: the store only updates it, so a missing Secret is a
  deployment fault (state `missing`, 503) that a login cannot fix. ActorJWT
  leaves no relay key to rotate.
  Once a device code has been exchanged, installing the login waits for any
  active refresh and retries persistence five times with a 10 s deadline per
  attempt. Cancellation of the device flow does not cancel these writes.
  Exhaustion reports `persistence_pending`; POST the login endpoint again to
  save the retained bundle without exchanging the code again. This recovery
  state is in memory and does not survive a broker restart.
- **GitHub.** Installation tokens from the factory GitHub App (key, App ID
  and installation ID mounted from the 1Password Secret). Every 30 minutes
  the broker mints one token per repository binding (`repositories=[repo]`,
  `contents: write`) and writes Secret `github-git` in the binding's
  namespace: key `basic` holds base64 of `x-access-token:<token>`, labelled
  `substrate.ate.dev/injectable: "true"`. The agent's git sends a placeholder
  `Authorization: Basic` header that the gateway replaces for
  `https://github.com`; Substrate's credential provider needs a RoleBinding
  in each binding namespace (step 8). The connector fetches its own token
  from `GET /github/connector-token?repository=<owner/name>` with a
  TokenReview-checked projected token: contents and pull requests write,
  checks and statuses read, and `merge_queues: write` [I]. A token must name
  exactly the repository and exactly the requested permissions (GitHub adds
  `metadata: read`); any other token is revoked and the mint fails. The
  broker only updates a `github-git` Secret labelled
  `app.kubernetes.io/managed-by=factory-broker`. `GET /codex/quota` serves
  `connectorSubjects` and `reviewerSubjects` the stored quota reading:
  `exhausted` (the relay's pre-check without its probe), `resets_at` and
  `received_at`. It never calls the backend.
- **Reviewer App.** `githubReview` configures the reviewer App
  (`jomcgi-factory-review`, "Merge authority") the same way, kept apart from
  the author's: it mints a read-only (`contents: read`) `github-git` Secret
  in each `reviewBindings` namespace, which must not be a coding binding's,
  and serves `GET /github/reviewer-token?repository=` to `reviewerSubjects`
  only, with checks and merge queues write, pull requests, contents and
  statuses read. The reviewer's PR comment needs a separate token with
  `issues: write` and `pull_requests: write`
  (`githubReview.commentPermissions`): GitHub refused a PR conversation
  comment from a token with `issues: write` alone (HTTP 403, 2026-11-15).
  `pull_requests: write` could approve, so that token never leaves the
  broker. `/github/reviewer-comments` (`github.CommentProxy`), for the same
  subjects only, does three things with it: finds the oldest comment
  `githubReview.commentAuthor`, the reviewer's bot login, wrote on a PR
  (reading up to 10,000 comments a page at a time and stopping at the first
  match, so the reviewer makes one request), creates a comment on a pull
  request (an issue number is refused), and edits a comment on a pull
  request that the bot login wrote. Lookup has a five-minute budget in both
  the reviewer and its broker client. The comment write gets a fresh
  30-second budget after lookup. It serves
  nothing else, and runs only when that login is set. An App without those
  permissions fails only the comment, and the check token never gains
  them. `merge_queues: write` alone does not let this token
  enqueue: the first live attempt was refused with `FORBIDDEN` (2026-11-14),
  so the reviewer enqueues nothing ("Merge authority"). With `reviewer.enabled`, the chart mounts the separate App from
  `reviewer.githubApp.itemPath`, derives `reviewBindings` from the review
  namespaces, and admits only `factory-reviewer` in `reviewerSubjects`.
  The config refuses the same App as `github`, a writable
  agent token, any reviewer write besides checks and merge queues, a
  comment token writing anything but issues, and a
  subject that is also a connector or admin subject.
- **Listeners.** The relay port admits only Substrate's egress gateway; a
  second port serves the admin endpoints, connector tokens, the connector's
  quota read and metrics, and
  admits the connector and, when enabled, the reviewer, so no actor can reach
  them. Operators use a port-forward. Cilium L7 rules could split one port instead, but Hubble
  would then record each relayed request's actor JWT.
- **RBAC** the chart grants the broker: `create` on `tokenreviews`; in its
  own namespace `get`/`update` on the grant Secret and the quota ConfigMap
  by `resourceNames`; in each binding namespace `create` on Secrets (RBAC
  cannot restrict `create` by name) and `get`/`update` on `github-git` by
  `resourceNames`. A ValidatingAdmissionPolicy closes the `create` gap: the
  broker's ServiceAccount may write only its grant Secret and Opaque
  `github-git` Secrets in binding namespaces, so it cannot mint a
  ServiceAccount token. Its image, like every first-party image, is private
  on GHCR; the chart syncs `k8s-homelab/ghcr-read-permissions` into the
  factory namespace as a pull secret.
- **Metrics** (`factory_broker_*`): grant state and expiry, refresh checks
  and the shared refresh counters, relay requests by outcome and status
  class, actor JWT rejections by reason, GitHub mints by kind, binding and
  outcome, usage endpoint reads by outcome, and the shared quota gauges.
  Labels come only from fixed sets or configuration.

## Identities

**Notification sink (#7935), staged off.** `connector.notify.enabled` wires
`connector/notify` to the private monolith's `monolith_agent_notify` through
Cloudflare at `factory-notify.jomcgi.dev`. The dedicated Authentik provider and
`factory-notify-sa` account grant only that tool at the default Discord channel.
1Password mounts its app password only into the connector. Its Cilium policy
admits the notify and token hostnames on TCP 443. The client projects fixed
fields, bounds text, deadlines and retries, and deduplicates caller keys per
UTC day through the durable outbox. A rollout sends a fixed `info` smoke after
acquiring the writer lock. Credential issuance, activation and live acceptance
remain in [the rollout task](deploy/notify.md).

**Why.** Operator and Ember agent credentials grant unrelated tools. A dedicated
issuer and monolith permission check confine this sender even if it guesses
another tool or channel. Durable dedupe handles the response-loss case that
a connector memory cache cannot.

### Factory front page

`factory.jomcgi.dev` shows Needs you, In flight, Shipped recently, then the
tool links (#7959). The connector serves `GET /status` on port 8082, separate
from metrics. Needs you lists Joe's tasks: open (backlog, unstarted or started,
not archived) issues assigned to `@joe` with no delegate, such as HOME-20 to
HOME-23. Agents never pick these up; delegating one is an explicit handoff, and
the page's reads start no runs. Joe's member ID comes from each bound project's
scaffold on every refresh. In flight is the store's active, blocked, review and
merging work items, and Shipped recently its ten latest merges. Titles come
from itsaplan; batched GitHub queries read checks, the reviewer check (or review
decision) and queue state. No issue bodies, check output, transcripts or tokens
enter the response.

The itsaplan contract is pinned to v1.10.0 commit `6c7c2fe2`: the
[project route](https://github.com/croffasia/itsaplan/blob/6c7c2fe2cb9d810d29c68e054de25876b47434f8/apps/api/src/modules/projects/index.ts#L143)
returns assignees with `userId`, nullable `username` and `kind`, and the
[issue listing](https://github.com/croffasia/itsaplan/blob/6c7c2fe2cb9d810d29c68e054de25876b47434f8/apps/api/src/modules/issues/index.ts#L462)
filters by `assigneeUserId` (at most 500) but not by a null delegate, which the
connector checks. Without a member `joe`, `needsYou` is null and only Needs you
shows as unavailable. On 2026-11-16 HOME's assignees included `joe` (`member`).

Status polling starts after writer acquisition. Every request, retries
included, checks the writer epoch, commits a `status_read` attempt, checks the
epoch again and only then reads with `X-Request-ID`. If an epoch check or the
journal write fails, nothing is sent. The message ID hashes method, URL and
query; retries and takeover keep it and count attempts. HTTP/1 without
connection reuse prevents unjournaled retries. TLS offers only `http/1.1`:
when the client also offered `h2`, GitHub chose HTTP/2 and closed each
HTTP/1.1 read, so `/status` returned 503 from its rollout in #7973 until
HOME-317. Writer loss cancels status and dispatch together.

A GraphQL query reads at most 25 PRs (75 connections, one
[point](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api#predicting-the-point-value-of-a-query)),
at most four per refresh with one auth retry each: at most 960 of the
5,000 points per hour. An over-budget refresh fails closed. The snapshot
refreshes every 30 seconds (20-second deadline, eight concurrent reads, at
most 100 live items and ten merges) and expires after 30 seconds. A truncated
listing, excess live work, a failed refresh or a stopped poller clears it, and
`/status` returns 503. Each failed refresh logs `status refresh failed` with
its stage (`needs_you`, `work_items`, `github_budget`, `github`, `titles` or
`other`) and a timeout flag, but no upstream error text. Visitors trigger no
upstream reads; the credential-free landing server proxies only this snapshot,
with a two-second timeout.

**Why.** The connector already has the store and coordinator read access. The
landing chart owns the status-only Service and Cilium grant, so removing the
page removes its access. The edge's sign-in and route stay unchanged.

### Authentication

| Who | How |
| --- | --- |
| Humans to itsaplan | Edge OIDC, then itsaplan's own OIDC client `factory-itsaplan` in Authentik; invite-only registration; a break-glass admin with a generated password |
| Humans to kagent | Edge OIDC with the access token forwarded; kagent in trusted-proxy mode. Sign-in alone grants nothing: the Authentik application binding admits only the `authentik Admins` group, and kagent session ownership limits each user to their own sessions |
| Connector to kagent | Projected ServiceAccount token (audience `kagent-invocation`) of the kept ServiceAccount `factory/factory-connector`, reviewed by kagent with TokenReview; principal `kubernetes:<authority>` and the ServiceAccount UID (finding 15) |
| Connector to itsaplan | A coordinator external agent for comments, human-gate issues, run lookups and moving merged issues to Done (`work_items` create, read and edit, `ai_agents read` in each bound project); the coding agent's key claims runs |
| Connector and agents to GitHub | Broker-minted App tokens, one repository per binding. The connector fetches its token with a projected ServiceAccount token (audience `factory-broker`, subject in the broker's `connectorSubjects`) and caches it until 15 minutes before expiry; a GitHub 401 refetches once |
| Independent review | During bootstrap, local review agents approving as a trusted reviewer (`jomcgi`); afterwards the reviewer App's required `factory review` check ("Merge authority") |
| Workloads | Substrate pod certificates and actor SPIFFE IDs; no SPIRE |

## Merge authority

Decided with Joe on 2026-11-14 as the target state. Writing code, reviewing
it and merging it are separate identities, and GitHub enforces the review
rather than the factory's own code.

**Status (2026-11-14): the reviewer is live (#7937) on gpt-6.1-terra
(#7938) and enqueues nothing (#7940). The connector waits for its check
(`reviewerCheck`, after #7940's connector deployed). Ruleset 9180009 then
requires `factory review` from App 5758326 beside `pr-checks`
(Joe's [decision](https://github.com/jomcgi-org/homelab/issues/7817#issuecomment-6882698416)).
Open work: #7920 (connector authorship check), #7921 (third-party Apps),
#7922 (in-cluster CI and registry) and #7923 (fork PRs and public
previews).

- **Author.** `jomcgi-factory` pushes branches and opens PRs: contents and
  pull requests write, checks and statuses read, and merge queue write for
  now. The connector enqueues the factory's PRs with it, because the
  connector sees their stops; GitHub's required `factory review` check gates
  them as it gates every PR. The agent's token is contents write on its own
  repository only. Neither can post the review check.
- **Reviewer.** A separate App, `jomcgi-factory-review`, with checks write,
  merge queues write, and contents, pull requests and commit statuses read.
  `projects/factory/reviewer` posts the `factory review` check run on every
  head of every open PR into the base branch, whatever its author, and on
  each merge group commit (success when the queued PR's head passed, since
  GitHub checks the group again; a passing group whose PR's head stops
  passing, say for a hold label, gets a failing check on the next poll). It enqueues nothing: the deploy values set
  `enqueueAuthors: []`. Its first live enqueue, of #7707 on 2026-11-14, got
  `FORBIDDEN: Resource not accessible by integration`. Its token has merge
  queues write, but GitHub evidently also wants contents or pull requests
  write, as the author App holds. Either would let the reviewer push code
  or approve, so Joe chose the required check as the gate instead. It never
  posts a PR approval, so it can never count
  toward an approval or code-owner rule. With `reviewer.commentAuthor` set
  to its bot login, it also keeps one comment on each PR it reviews, created
  with the first verdict and edited in place on every head, approval and
  hold: the verdict, the human gate's reasons, the agent's summary, findings
  by severity and what the review cleared. The comment mirrors the
  assessment behind the check and gates nothing. It is written with the
  broker's comment proxy, which holds the comment token (`issues: write`
  and `pull_requests: write`), so an App without them loses only the
  comment; a failed write is logged and counted
  (`factory_reviewer_comment_writes_total`) and retried on the next poll.
  It edits only a comment its bot
  login wrote, and its own comment never reaches the agent.
  Check runs hold review state; the private audit volume keeps goal replies. The
  in-progress run names its attempt and kagent task, and a completed one
  keeps its blocking findings, human gate and kagent session in a hidden
  block, at most 48,000 characters, for the next head. A compressed comment
  snapshot shares the check text's 60,000-character budget; oversized
  snapshots link to the full findings in the check. Gate changes carry the
  snapshot's findings even if the earlier comment is still queued.
  After a restart, completed checks replay their snapshots through the
  comment worker after gate reconciliation. This recovers writes lost when
  a rollout kills a long comment lookup. Duplicate ready PRs
  stop model work; duplicate or foreign checks require a fresh review and human approval.
  Holds survive new commits; complete label history recovers failed writes.
- **Restarts.** A restart loses only the shared-head observations whose
  check writes all failed: GitHub keeps label history, and every other hold
  is in a check run's state. The first poll to see a PR opened before
  startup (by GitHub's latest all-state PR creation time, so reopened PRs
  count) looks for that loss. Every head in the PR's timeline is matched
  against the open PRs into the base and every PR updated since the PR
  opened, in any state and base; GitHub's commit lookup adds the open PRs
  that contain its current head. A match holds the PR as a shared head. A
  truncated timeline, an earlier review the search cannot find, or a
  listing past its bound holds it as an unconfirmed context until merge. A
  failed read gives the PR no pass, enqueue or merge group authorization,
  and the next poll reads again. Newer PRs skip recovery unless their check
  is foreign. A hold an earlier process placed for its restart alone clears
  once recovery confirms the context; a hold with any other reason stays as
  it is. Recovery holds record up to 64 review IDs; a new ID or verified
  nonce clears them despite clock skew. Nonces also resolve ambiguous
  timestamps and overflow; author, editor, state, commit and PR must match.
  Revoking a pass requires a GitHub write.

  **Why.** Holding every existing PR on each restart made each reviewer
  deploy into work for Joe: after about eight deploys on 2026-11-15, 18 of
  the 23 open factory PRs carried the hold and 5 waited on it alone
  ([HOME-107](https://factory-plan.jomcgi.dev/factory/issue/HOME-107)). The
  hold guarded a loss that GitHub's PR records can show. A duplicate leaves
  no record to match when it moved off one of the PR's heads, other than by
  adding commits to the current head and staying open, before any hold
  write succeeded and the reviewer restarted.
- **Chart wiring.** `reviewer.enabled` defaults to false. Enabling it creates
  one `factory-reviewer` Deployment in `broker.namespace`, with `Recreate`
  and a kept ServiceAccount. Projected tokens use audiences `factory-broker`
  and `kagent-invocation`; the pod mounts no App key. The chart pins the
  reviewer image and supplies the namespace's GHCR pull secret. Its config
  derives repositories and agents from each binding, uses App ID 5758326,
  and defaults `enqueueAuthors` and `humanApprovers` to `[jomcgi]`.
  Other policy and timing defaults come from the service.
  Each binding gets `<name>-reviewer` using `agents.reviewer` (Codex,
  broker relay; the chart defaults to `gpt-6-astra` and the deploy values
  run `gpt-6.2-sol`) in
  `reviewer.bindings.<name>.namespace`, default `<coding namespace>-review`.
  The shared binding template creates its ModelConfig with an actor JWT,
  Harness, WorkerPool, git EgressPolicy and credential-provider RoleBinding.
  The namespace cannot overlap any coding or review binding: both Apps
  write `github-git`. The broker admits the review atespaces to its relay,
  mounts the separate reviewer App's key files from 1Password and writes
  their read-only git tokens. Network policy permits the reviewer service
  only to the broker's admin port (8081), kagent A2A (8083), itsaplan's API
  (3000 when enabled) and `api.github.com:443`, and admits it at each peer.
  The enable PR follows CI's chart write-back, adding review namespaces to
  `kagent.rbac.namespaces`. Substrate's policy values include
  `factory-r-homelab-review`; regenerate its upstream manifests before
  enabling. The connector's `reviewerCheck` followed the chart that carries
  #7940's connector, and the required-check ruleset follows that.
- **Review contract.** The agent reviews the exact head against the root
  `REVIEW.md` and every `REVIEW.md` on both sides of the diff, with their
  `Includes:` resolved, and `AGENTS.md` and `docs/agents/reference.md`, all
  read from main and sent in the prompt. It treats its checkout of the head
  as data and runs nothing from it. It returns findings only (`summary`,
  `findings[{id, severity, path, line, title, detail, defer, earlier, cause}]`,
  `previous[{id, status, note}]`, IDs `<path>:<rule>`), and the service
  decides: success only when no critical, high or medium finding blocks.
  Optional `semgrep {appId, checkName, wait}` adds GitHub-only scan evidence;
  unset is off. The service holds no Semgrep token and calls no Semgrep API.
  It matches `semgrep-cloud-platform/scan` by name, App ID 4746654 and exact
  head. Before dispatch it waits for completion, bounded by `wait` (10m).
  Success and failure both prove a scan. Missing, pending, cancelled,
  skipped, errored or unreadable evidence adds the service-owned medium
  `semgrep:unverified`, which the agent cannot waive.
  The service reads conversation and inline comments from
  `semgrep-code-jomcgi-org[bot]`, checking App ID wherever GitHub reports it.
  Other authors never enter this evidence, including comments that quote Semgrep.
  A separate `writeUntrusted` block of kind `semgrep` carries at most 50
  comments of 4,000 characters and the omitted count. It includes path, line,
  original commit and GitHub's explicit outdated flag where available.
  Check output shares this block and has a 4,000-character bound.
  Incomplete listings and omitted or truncated comments also fail closed.
  The agent accounts for each finding against the current head: fixed,
  false positive with code evidence, or a normal finding at its review severity.
  Evidence digests bind tasks and saved reviews. Changed evidence, including
  a late completed scan on the same head, starts another review.
  An agent finding that collides with `semgrep:unverified` keeps its ID and
  severity. Service availability state stays separate. Enablement follows CI's chart
  write-back in a separate PR.
  An attempt without a usable report (unparseable output, an unknown
  severity, a failed task other than quota exhaustion, a rejected send or
  a timeout) is retried on the same head after `retryBackoff` (5m); the
  third such attempt fails the check. A later head's prompt carries the
  previous blocking and `human-gate` findings and the interdiff; each comes
  back `fixed`,
  `justified` (a medium with a note, or any severity with a note starting
  `withdrawn`) or `open`, and one left out stays open. One reported again
  keeps the higher of its two severities unless it is fixed or withdrawn,
  except a goal finding reported again as `after_rollout` (see the goal
  check below). A
  `human-gate` finding not fixed or withdrawn holds the later head too.
  The check's State also keeps up to 20 findings earlier reviews cleared
  (fixed, justified or withdrawn, with notes). The prompt lets the agent
  raise a fixed one again whenever it is present (a regression), and a
  justified or withdrawn one when a change since the previous head
  affects it or when it is present at high or critical severity. It asks
  the agent to check each again when the interdiff is unusable. On a later
  head each new or reopened finding carries a `cause`: the changed file, and
  how the change introduced or exposed it. When the head descends from the
  previous one and GitHub lists the whole interdiff, the service reads each
  file's hunks from its patch (a file without a patch counts as changed
  throughout). It lowers a new medium finding to a deferred low one when
  the finding's line is outside every changed hunk and its cause names no
  changed file, or when the agent marks it `earlier`
  ([HOME-107](https://factory-plan.jomcgi.dev/factory/issue/HOME-107),
  [HOME-250](https://factory-plan.jomcgi.dev/factory/issue/HOME-250)). A
  caller broken by a changed function keeps its severity through its cause.
  After a rebase, the service compares the PR's own diff against the base
  before and after, and counts as changed only the files
  whose diff changed; any stated cause counts, since the base moved too.
  The check shows each cause, marks a new high or critical finding without
  one as an earlier review's miss, and names a reopened finding's earlier
  status. A finding whose ID an earlier review recorded as fixed keeps its
  severity (a regression). So does a high or critical finding (REVIEW.md
  lets those block on earlier-reviewed lines), a finding with no path or no
  line in a changed file, and a finding under any rule but the root rules
  `correctness`, `security`, `boundary`, `reachability`, `validation`,
  `rollout` and `goal`: scoped rules set their own severity, and the
  review-process rules are not about earlier-reviewed code. Each blocking
  finding is a defect, missing evidence (`unverified`, `unreviewed`, an
  `unclear` goal verdict, unverified issue evidence) or a hold (the human
  gate, `semgrep:unverified`, an unread issue, an unavailable goal check),
  and the check says what each kind asks for. A reattached
  review waits while it cannot read the interdiff. When GitHub no longer has
  the earlier head, it reviews the whole change again in a new attempt.
  **Why.** Each head gets a fresh kagent session that read the whole diff
  again and found new problems in code the earlier review had covered; 18
  PRs used all three review rounds with blocking findings left.
  When the earlier review cannot be found within the search bounds, the
  prompt and the check say so and the head is reviewed in full. A diff over
  budget (2500 lines or 150 files, generated files excluded by their new
  path) fails with "split this PR" and sends no task. Its summary ends with
  the service's `split-only` marker, so the connector counts that round
  against `maxSplitRounds`; the conclusion is unchanged. The prompt carries the
  PR's commit messages in a delimited `commits` block: those since the earlier
  reviewed head while it is still in the PR's history, else all of them, the
  newest 20 within 16,000 characters and 4,000 per message, read from
  `GET /pulls/{n}/commits` at the head. The agent weighs a "Review response"
  in them as the author's claim and checks the evidence it cites before it
  counts a finding justified or withdrawn. A failed read is named in the
  prompt and the review goes ahead. The prompt also carries the
  PR's text and file names from any author, quoted inside delimited
  untrusted blocks (`REVIEW.md` "What the agent sees"). Each delimiter names
  the head and a 128-bit digest of its block's text: holding its own closer
  would take a SHA-256 preimage search, so even a look-alike delimiter the
  sanitizer misses cannot close a block in practice.
  The reviewer's agent has no GitHub API access, so the service reads the
  evidence a description cites: up to eight `#N` or same-repository PR links,
  in order. For each pull request it reports only fixed classes: state,
  merge time and commit, whether the timeline shows a merge queue entry and
  who last added it, who last enabled auto-merge (the timeline event, which
  outlives the merge), logins that match GitHub's login syntax (others become
  `(unrecognized login)`), the head commit's author account and signature
  state, and the review check and the binding's required checks on the head,
  each matched by name and creating App or status creator. Titles, bodies
  and branch names are never read. The list sits in its own delimited
  `cited` block. A number that names an issue or nothing reads as "not a
  pull request" (`pullRequest(number:)` needs no issues permission), and a
  failed read marks that PR unread instead of holding the review.
- **Issue evidence (HOME-58).** The service reads issue and decision
  evidence only for PRs by `jomcgi-factory[bot]` or a `humanApprovers`
  login; any other PR gets none. For the
  factory App it resolves the connector's branch; PR text cannot override
  it. Other PRs use the first `Refs HOME-N` in the body, then commit
  messages in PR order, read once per head.
  The code-review prompt gets no issue or decision text. It reviews the
  diff, title and description as on a PR with no issue, and the
  description stands in for the goal.
  v1.10.0 exposes creators through the creation activity's user ID, matched
  to the project's account metadata. Only member accounts in
  `trustedItsaplanAuthors` (default `joe`, `joesessions`) carry authority;
  `factoryadmin`, agent accounts and claims in text do not. Creation
  events contain no original text, and issue edits are not restricted to
  the creator. Current title and description carry authority only when
  both match their latest immutable text-change snapshots, also written by
  trusted members. History reads stop at ten pages; absent or mismatched
  snapshots leave the text untrusted. This includes newly created issues
  with no text-change snapshots: authority then requires an unedited
  trusted decision comment.
  Comments must have an explicit null `editedAt`: edits retain the original
  author and do not identify the editor. Workflow status remains untrusted
  metadata.
  These defaults implement
  [HOME-58](https://factory-plan.jomcgi.dev/factory/issue/HOME-58); Joe's
  sessions account is provisioned by HOME-59 as `joesessions` (itsaplan's
  human username validator excludes hyphens).
  Per evidence snapshot the service fetches one issue body (16,000 bytes,
  title 300) and at most ten references from both sides of the diff. Linked
  comments are cut to 4,000 bytes; other issue links use untrusted search
  metadata without importing another description. Bare issue authorship
  never proves an unseen decision. Comment reads search at most
  ten feed pages because upstream has no individual comment GET. The whole
  evidence read has a 30-second deadline. A
  failure that no read can fix (an issue over its text bound, a reference
  to no issue, a diff past the link or patch bound) is a medium `unverified`
  finding in the completed review, which the author or Joe resolves. Its
  text is the service's own, with the issue key at most. A read
  error holds the check with the failed reads named and the gate-only
  marker, so the connector starts no revision round for it. No failed read
  supplies a pass. The agent receives no itsaplan key.
  Running and completed checks record the evidence digest, an HMAC-SHA256
  under a key derived (HKDF) from the reviewer's itsaplan key. The check is
  public, so an unkeyed hash would confirm a guess at private text. The service
  re-reads evidence before accepting a report and on every completed-check
  poll before enqueue or merge-group authorization. Changes or read failures
  revoke the old review, retain its findings and human holds, and schedule a
  fresh review of the same head once reads succeed. Waiting reads release
  review slots and retry on the next poll without dispatching model work or
  requiring a new push. The digest and pending state survive restarts and
  human-gate reconciliation; a legacy pass without a digest needs review.
  A merge group's pass lasts until the first poll after an evidence change
  (30 seconds by default). GitHub can merge the group in that gap, as it
  can after a hold label lands on the PR. Joe accepted this window.
  **Why.** The `factory review` check is public and itsaplan is private.
  Joe chose trusted authors on 2026-11-16
  ([decision](https://github.com/jomcgi-org/homelab/pull/8116#issuecomment-6930546469),
  [record](https://factory-plan.jomcgi.dev/factory/issue/HOME-58#comment-1047)).
  He then chose that no model output that has seen private issue text may
  reach public output
  ([decision](https://github.com/jomcgi-org/homelab/pull/8116#issuecomment-6931275436)):
  the code review sees none, and the goal check below is the evidence's
  one consumer.
- **Goal check (HOME-58).** A second model call checks the PR against its
  issue, apart from the code review. It runs after the code review gives a
  usable report, and only when a trusted author's PR names an issue that
  resolves with its full text. Its task opens a fresh session of the
  review agent with message ID `<check external ID>/goal/<prompt HMAC>`,
  so a restart finds the same task. No review resumes that session. The
  service deletes it after saving the full reply to the private audit volume.
  The prompt holds the issue
  and its status, the linked decisions with their provenance, and the file
  list and patches (at most 60,000 bytes). It holds no PR title,
  description or comment, so only the head and the evidence digest decide
  the answer. Trusted text is quoted. Other text sits in delimited
  untrusted blocks, with each of its lines quoted. The answer is one JSON
  block: a verdict for each acceptance criterion by number, and an overall
  verdict for the stated problem. The verdicts are `met`, `not_met`,
  `after_rollout` (only a rollout or live use can show it) and `unclear`.
  When the issue plans later enablement, implemented criteria behind that
  switch are `after_rollout`. The overall verdict follows the same rule.
  Missing implementation is still `not_met`. Only the issue or a trusted
  decision establishes staging; a claim in the diff cannot defer a criterion.
  Any other field or value, no overall verdict, numbers other than 1 to N
  once each, more than 50 criteria or a second value voids the answer. The
  service uses only those fields for findings, and posts only its own text:

  | Answer                      | Finding ID                | Title                                         | Severity |
  | --------------------------- | ------------------------- | --------------------------------------------- | -------- |
  | overall `not_met`           | `HOME-N:goal`             | HOME-N: stated problem not addressed          | high     |
  | overall `unclear`           | `HOME-N:goal`             | HOME-N: stated problem unverified             | medium   |
  | overall `after_rollout`     | `HOME-N:goal`             | HOME-N: stated problem to check after rollout | low      |
  | criterion K `not_met`       | `HOME-N:goal:criterion-K` | HOME-N criterion K: not met                   | medium   |
  | criterion K `unclear`       | `HOME-N:goal:criterion-K` | HOME-N criterion K: unverified                | medium   |
  | criterion K `after_rollout` | `HOME-N:goal:criterion-K` | HOME-N criterion K: to check after rollout    | low      |
  | void, failed or timed out   | `HOME-N:goal:unavailable` | HOME-N: goal check unavailable                | medium   |

  The delivery owner checks each `after_rollout` verdict after rollout. The
  goal check owns these IDs: the service drops the code review's findings
  and accounts under them. An earlier goal finding stays open at its
  earlier severity until a valid answer on the same issue resolves it. One
  reported again as `after_rollout` takes that verdict's low severity: like
  `met`, it says the diff makes the change it needs, and the delivery owner
  checks the rest after rollout (HOME-179). Other verdicts keep the earlier
  severity, so an `unclear` answer cannot lower a high `not_met` to a medium
  that a note can justify. The agent cannot mark a finding as a goal
  verdict, so its own findings keep the rule.
  `met` resolves its criterion or the stated problem, and any valid answer
  resolves `unavailable`. An answer that omits criterion K, or a goal check
  of another issue that the PR names now, leaves the finding open. Only the
  issue text or a linked trusted decision moves a criterion to another
  issue. HOME-157 adds service-verified follow-ups, removed criteria and
  issue switches. If the code review has no blockers apart from the human
  gate, any `not_met` gets one confirmation call (HOME-158). That call opens
  a fresh session with the same evidence and a request to check planned
  staging. It receives neither the first reply nor the code review's prose.
  It must judge every criterion from the initial answer; an omitted criterion
  makes the confirmation unavailable.
  A valid confirmation supplies the verdict only for the initial `not_met`
  ones, including a confirmed `not_met`; it cannot turn a `met` into a
  `not_met`.
  A failed confirmation produces the medium `unavailable` finding; it cannot
  publish the first answer's high. Existing blockers and human holds still apply.
  The check's State caches the final answer for its head and evidence digest.
  A fresh review of that evidence reuses it; unavailable answers are not cached.
  A later head on the same evidence digest starts from the previous head's
  answer: the prompt carries its verdicts, its private reply from the audit
  volume (quoted, at most 8,000 bytes) and the files changed since, and asks
  the model to keep each verdict unless a change affects it. An `unclear`
  answer keeps the previous head's `met` or `after_rollout`, since it shows
  no failure; a `not_met` stands. The prompt says that CI shows whether a
  test passes: a criterion that asks for a test is `met` when the diff adds
  it with a build target CI runs.

  ```mermaid
  flowchart LR
    G[Goal reply] --> A[Private audit volume]
    A --> P[Parse numbers and verdicts]
    P -->|initial not_met and no code blockers| C[Fresh confirmation session]
    C --> A
    P -->|final verdict| S[Service wording]
    S --> H[Public GitHub check and comment]
    R[Kept hourly retention job] -->|remove expired records| A
  ```

  **Private audit (HOME-158).** PVC `factory-reviewer-goal-audit` provides
  1 GiB on the default storage class. Only the reviewer and its cleanup job mount it.
  JSON files under `/var/lib/factory-reviewer/goal-audit` keep each full final
  reply, including its rationale and malformed answers. Each record names
  the repository, PR, head, issue key, evidence HMAC, message, task, session,
  task state, receipt time and phase (`initial` or `confirmation`).
  The filename hashes the message and task IDs. Retries keep the first record
  and timestamp; a replacement task gets its own record.
  The service syncs the file and directory before deleting the session or
  accepting the verdict. A failed audit write leaves the session intact and
  gives a medium `unavailable` finding. Storage errors log only fixed text.
  The directory has mode `0700`; files have mode `0600`.

  Retention is 14 days from first receipt. The service prunes at startup and
  hourly. CronJob `factory-reviewer-goal-audit` also prunes hourly with the
  packaged reviewer image and `--prune-goal-audit`. That mode needs only the
  volume; it reads no service configuration or credentials and makes no network calls.
  Its kept NetworkPolicy admits no ingress or egress. The job prefers the
  reviewer's node for RWO disks, but can run without a reviewer Pod.
  Expired temporary writes are removed; fresh writes are left for the active writer.
  Concurrent pruners tolerate files that the other process already removed.

  The PVC, CronJob and its NetworkPolicy have `Prune=false,Delete=false`
  and Helm's `keep` annotation. A rollback to a chart without audit support
  preserves unexpired replies and the independent retention job with its pinned image.
  Deletion can lag expiry by one hour while the cluster runs. After downtime,
  the next job prunes expired files; the reviewer also prunes before starting.
  Keep these resources after rollback. Decommission them only after the last
  audit write is 14 days old and a cleanup job completes successfully.
  The chart configures no backup or public read endpoint.

  **Goal reasons for the delivery owner (HOME-251).** With
  `reviewer.goalReasons.enabled` (off by default), the reviewer serves
  `GET /v1/goal-reasons?repository=&pullRequest=&head=` on its own port
  (8091), apart from `/metrics`. It answers with the newest reply for that
  head, without its JSON answer and clipped to 4,000 bytes, with the issue
  key, evidence digest and phase, or 404. Its Service exposes only that port,
  and the reviewer's CiliumNetworkPolicy admits only the connector to it. The
  chart refuses to render it without the connector, itsaplan and Cilium
  policies. When a review round acts on the reviewer App's check with a goal
  finding of the work item's issue, the connector checks the writer epoch,
  records the read in `delivery_read`, and reads that head's reasons. Reasons
  for another issue, a 404 or a failed read leave them out, and the round
  starts on the check's fixed text. Otherwise the round prompt quotes them,
  line by line, in an `untrusted_goal_reasons` block, and the outbox posts
  them once per head on the issue as the connector's agent account. Both
  copies replace `@` with `＠`, so no text mentions a member or starts an
  agent, and drop any tag that could close the block. No reason reaches a
  GitHub check, PR comment or log.
  **Why.** Fixed public wording left coders guessing which part of a
  criterion failed. Joe chose the issue as the record and the connector as
  the only writer
  ([decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-251#comment-2324)):
  itsaplan 1.10.0 gates comments on `work_items` edit, so a reviewer write
  token would also let it edit and delegate issues.

  Operators with private Pod access can find a PR's records with:

  ```sh
  kubectl -n factory exec deploy/factory-reviewer -- sh -c \
    'grep -l "\"pull_request\":8122," /var/lib/factory-reviewer/goal-audit/*.json'
  ```

  Read the matching files in a private operator console. Never copy replies
  to GitHub, service logs or public artifacts. Review agents receive no mount.
  **Why.** Joe chose structured verdicts on 2026-11-16
  ([decision](https://github.com/jomcgi-org/homelab/pull/8116#issuecomment-6931275436)): fixed wording from enum values cannot carry
  issue text, whatever the model writes. HOME-158 adds a private audit after
  the staged Semgrep implementation in #8122 received a false `not_met`.

- **Human gate.** Joe decided the gate on 2026-11-14 (#7817,
  [decision](https://github.com/jomcgi-org/homelab/issues/7817#issuecomment-6878730211))
  and narrowed it the same day to what could weaken the gate itself
  ([decision](https://github.com/jomcgi-org/homelab/issues/7817#issuecomment-6884458635)):
  under the first scope almost every factory PR needed his bypass, which
  skips CI and the review. A PR touching `REVIEW.md`, `**/REVIEW.md`,
  `.github/CODEOWNERS`, `projects/factory/reviewer/config/**`, the
  reviewer's `review/report.go`, `review/gate.go`, `review/rules.go` or
  `review/issues.go`, or
  `projects/factory/broker/config/**`, or labelled `human` or
  `needs-human`, gets a
  `human-gate` high finding from the service; the agent reports the rest of
  `REVIEW.md` "Joe decides" under the same rule, matched by any rule segment
  of the ID that starts `human-gate`, at any severity. Factory-authored PRs
  require `jomcgi` to approve that exact head. Joe's
  [authorship decision](https://github.com/jomcgi-org/homelab/issues/7817#issuecomment-6891640540),
  recorded by `jomcgi` on 2026-11-15 and scoped in
  [#8049](https://github.com/jomcgi-org/homelab/issues/8049),
  also permits a PR from a `humanApprovers` account with its head in the base
  repository, when every push since the head ref's latest creation is by
  those accounts and the newest push matches the head. The reviewer reads
  `/repos/{owner}/{repo}/activity?ref=refs/heads/<branch>` on every poll,
  requires complete history through creation within 100 events, and grants
  no waiver on a read error or SHA mismatch. It names this approval by
  authorship in the check output and never stores it as an approval.
  Git commit authors such as `ci-format-bot` do not identify push actors.
  Renovate is excluded by `renovate/` head branches or any PR commit with
  its git author email, read from base `renovate.json` `gitAuthor` when set;
  `renovate@whitesourcesoftware.com` and the maintenance workflow's
  `renovate[bot]@users.noreply.github.com` are always excluded. A separate
  non-admin Renovate identity remains follow-up work. The waiver checks
  GitHub account provenance, which Joe's decision also accepted for other
  traffic on his account. GitHub activity carries no evidence of session
  supervision. His recorded condition was "agents can automerge if I ask
  them to". Sessions Joe guides land gated changes only when he asked in
  that session. Hold labels, shared-head, foreign-check and restart-recovery
  holds still require an
  APPROVED review. Other findings still block. A hold is logged once as `human gate`
  and marked so the connector can skip its revision round.
- **GitHub enforces it.** Ruleset 9180009 will require `factory review`, bound to
  the reviewer App's ID, beside `pr-checks`. No pull request merges into main without
  a passing review on that head, including PRs from Joe, interactive
  sessions, Dependabot/Renovate and other third-party Apps such as Polylane.
  Third-party Apps may open PRs but merge only through this gate. Authors
  enqueue: the connector does so for the factory's PRs once the check passes,
  and other authors use auto-merge (`gh pr merge --auto`), which GitHub
  queues once the required checks pass. The ruleset's only bypass
  actor is the organization admin role: Joe, for emergencies, and today the
  chart version write-back, which pushes to main with an identity holding
  that bypass (`bazel/helm/write-back-versions.sh`); in-cluster CI (#7922)
  should replace that identity with one scoped to the write-back. Changes
  that define the gate itself need Joe's approval (`REVIEW.md` "Joe
  decides").
- **Trigger.** kagent v1.0.1 has no trigger resources (no webhook, cron or
  event types); agents run when sent an A2A task. A small deterministic
  `factory-reviewer` Deployment, separate from the connector, with its own
  ServiceAccount, polls open PRs into main every ~30 s. It uses ETag
  conditional requests on the reviewer App's rate pool and needs no public
  webhook endpoint. For a new head SHA it creates the `factory review` check
  as in progress, with external ID `factory-review/<owner>/<repo>/<pr>/<sha>`,
  and sends the kagent `reviewer` agent an A2A task with message ID
  `factory-review/<owner>/<repo>/<pr>/<sha>/<check run ID>`, idempotent like
  coding runs; a retry adds `/attempt-<n>`. A later head's first attempt
  resumes the previous head's session and adds `/resume` (below); every
  other attempt opens a fresh session. Each
  attempt's message is built once and resent byte for byte: at most 20
  sends, then one more after a dispatch window. When
  no open PR still has a head, its task and check are cancelled: at once
  when the PR moved to another head, and after two complete listings in a
  row leave the PR out when it closed, since a PR closing between page
  reads shifts another off both pages. The
  agent has its own binding, a read-only repository token and no push. The
  service then completes the check on that exact SHA from the agent's
  findings and, on a pass with required CI green, enqueues Joe's PRs. The agent never
  holds the reviewer App's credentials, and the broker mints the author's and
  the reviewer's tokens only for their own ServiceAccounts.
- **Quota wait (HOME-110).** The reviewer checks the diff budget before
  waiting for quota, including when reattaching to a waiting check. An
  oversized PR receives a split request without a Codex task. Before
  sending a review, the service reads the broker's `GET /codex/quota`.
  An exhausted grant or a failed quota read sends no task. A failed task that matches the connector's quota
  classifier also waits without spending an attempt. The check stays
  `in_progress` with title "Waiting for Codex quota"; the worker is released.
  The check records the wait time and a separate quota retry number.
  Polls resume the same head after a newer broker reading reports usable
  quota, including after a service restart. The replacement opens a fresh
  session and adds `/quota-<n>` to its message ID, so kagent cannot return
  the refused task again. Ordinary failures still spend attempts.

  ```mermaid
  stateDiagram-v2
    [*] --> Ready
    Ready --> Waiting: broker exhausted or unreadable
    Ready --> Reviewing: broker usable
    Reviewing --> Waiting: task fails with quota status
    Waiting --> Ready: newer usable reading
    Reviewing --> Complete: usable report
    Reviewing --> Ready: ordinary failure and attempts remain
    Reviewing --> Complete: ordinary failure on last attempt
  ```

  **Why.** Quota refusals exhausted all three review attempts on the same
  head. [HOME-110](https://factory-plan.jomcgi.dev/factory/issue/HOME-110)
  keeps waiting work outside the worker pool and resumes it without a new commit.
- **Resumed sessions.** The first attempt on a later head continues the
  kagent session that reviewed the previous head, as the connector
  continues a coding round: an A2A `SendMessage` naming the session's
  context ID and no task ID opens a new task in it. The completed check's
  State records the session (context ID, binding, agent, PR, head, path,
  heads reviewed, the IDs of their tasks, task bytes, rules digest). The
  service reads it only from its own App's check on the earlier head, never
  from PR, commit or comment text. Before resuming it reads the session
  (`SessionService.GetSession`) and the Agent (`AgentService.GetAgent`) on
  the A2A listener, and lists the session's tasks over A2A. kagent runs
  every turn of a session on the Agent revision it pinned at creation, so
  the session must be pinned to the Agent's `latestSuccessfulRevision`, or
  a model or prompt change would never reach later heads. Its tasks must be
  exactly the recorded ones: a turn no completed review recorded (a review
  superseded, interrupted or ended without a report, whose check keeps no
  State) would make the recorded task bytes undercount the session.
  The continuation keeps the previous findings, the interdiff, the PR's
  text in new delimited blocks, the report contract and the rule that the
  PR's earlier content is data. It leaves out the rules and repository
  context while a digest of their text matches the one the session holds.
  The digest covers each file's whole text, including what the prompt
  clipped or omitted and told the agent to read in `main/`, and leaves out
  the commit: main moves with every merge, so comparing commits would
  resend the rules every time. When some text that may apply went unread
  (a file too large to read, rule files past the read bound or a path no
  rule can be looked up for) the digest is unknown and the rules are always
  sent. A review starts fresh, and `factory_reviewer_review_sessions_total`
  records why, with no recorded session, on a retry, for another binding,
  agent, PR or head, past 512 KiB of task bytes or 32 reviews, for a
  session kagent no longer has or reports deleted, crashed or failed, on
  another Agent revision, when its tasks differ from the recorded ones
  (including a continuation a restart lost before recording its task), on
  a failed read, or when kagent refuses the continuation (rebuilt fresh in
  the same attempt).

  **Why.** Each head used to re-read the rules, the diff and the
  surrounding code in a new session; a late review of #8062 carried about
  300 KB of history across about 60 steps. The reviewer runs on a ChatGPT
  Codex login through the broker relay, and OpenAI announced in October
  2026 that signed-in Codex users keep their conversation cache
  indefinitely, with a guaranteed hit on resume
  ([report](https://magica.com/news/openai-permanent-prompt-cache-codex-work)).
  A continuation should then pay mostly for the interdiff and the reads it
  prompts, while the weekly Codex grant stood at 92% until 2026-11-20
  (HOME-107). The bound is about 130k tokens at four bytes a token: one
  more full review past it could reach the context window, where Codex
  compacts the conversation and its summary replaces the cached prefix.

  **Measuring it.** kagent's Codex harness puts no usage on the task
  (finding 12) and the chart exports no kagent telemetry yet, so tokens per
  review cannot be read today. The service records each completed review's
  task bytes by path (`factory_reviewer_review_task_bytes`: prompt, replies,
  tool calls and results) and logs them as `review usage` with the context
  and task IDs. `factory_reviewer_review_tokens_total` splits uncached
  input, cached input and output once kagent reports them
  (`factory_reviewer_review_usage_total` counts reviews without). Compare
  mean task bytes per review, resumed against fresh, over the 72 hours
  after rollout, and the Codex weekly window's use per completed review in
  the broker's quota view over the 72 hours before and after; coding runs
  share that window. Resumed reviews withdrawing or missing findings more
  often than fresh ones is the regression signal, and lowering the
  reviewer's image pin reverts it.
- **Session cleanup.** Each kagent session keeps a Substrate snapshot
  (about 340 MB) until the session is deleted. After each send, the service
  names the task's session `factory-review/<owner>/<repo>/<pr>`
  (`UpdateSessionName`). Every 10 minutes, after a complete listing of open
  PRs, it lists its own sessions of the binding's agent (`ListSessions`)
  and deletes (`DeleteSession`, without force) the sessions no review will
  resume:
  - every session of a PR that is no longer open;
  - every session of an open PR except its newest. A later head resumes
    only the session the last review recorded, and that session is the
    newest unless every later attempt ended without a report.
  - an unnamed session (opened before this change, or whose name write
    failed) with no task change for 12 hours;
  - a session left DELETING by a delete kagent admitted but did not finish.

  The service keeps a session that is younger than 15 minutes, is named for
  another repository, has a task that still executes, or whose tasks it
  cannot list. kagent also refuses to delete a session during a turn. A
  partial session listing deletes nothing, because an unlisted session may
  be its PR's newest. Deletes run beside the poll loop and are counted in
  `factory_reviewer_sessions_deleted_total` by reason (closed, superseded,
  idle, retry). A deleted session that a later head would have resumed only costs
  a fresh review (`missing` or `deleted` above).

  **Why.** On 2026-11-16, 398 reviewer snapshots (117 GiB) filled node-2's
  root disk. The kubelet then evicted Substrate's Postgres and every agent
  stopped (HOME-136). kagent deletes a session only at its idle TTL (7
  days), and a shorter TTL would also expire coder sessions the connector
  holds for fencing. The connector deletes its own finished sessions
  ("Finished sessions" under Connector).
- **Connector.** It coordinates sessions, revision rounds and itsaplan
  status, and enqueues the factory's PRs once the required checks pass; the
  reviewer's failing check becomes the revision round's feedback
  (`reviewerCheck {name, appId}`), with its summary in full (at most 60,000
  characters, so the findings after the overview survive). A failure that
  the reviewer App marks `human-gate-only` starts no round: the PR waits for
  Joe's approval, which turns the check green. A `human` or `needs-human`
  label holds a PR at the connector too: labels are read at enqueue, so a
  hold placed after a passing review cannot race the reviewer's next poll.
  A `human` label observed by the connector while queued stops delivery and
  dequeues the PR; label removal does not resume it. A decision hold on a
  queued PR is also the reviewer's: on its next poll it fails the
  merge group's required check, and the queue drops the PR. The queue's
  `ALLGREEN` grouping keeps the PR from merging inside a later group that
  contains it. Joe's approval of the head clears the reviewer's hold. The
  connector never re-enqueues an ejected head, so Joe requeues it; the
  label still holds the factory's enqueue of any new head.
- **Only the factory's own loop acts.** Work starts only from itsaplan
  (invite-only, behind Authentik); GitHub issues, PRs and comments never
  start a run. The connector adopts and enqueues only PRs whose author is the
  factory App and whose head branch is the bound repository's
  `<branchPrefix><issue key>`; a fork or other author is refused and
  reported. Today adoption matches the bound repository's own head
  (`<owner>:<branch>`), which excludes forks; the author check is #7920.
  Review feedback reaches a coder's prompt only from `trustedReviewers` or
  the reviewer App. The reviewer and planner exceptions are in `REVIEW.md`.
- **No third-party writers in the trust path.** No other App holds
  `administration` (it can edit the ruleset) or bypass, and the required
  checks come from identities the factory runs. Build and artifact
  publishing move into the cluster: CI runners and an OCI registry for
  images and charts, with a documented bootstrap that does not need the
  registry it rebuilds. GitHub stays the forge.
- **Public routes** serve only mock or fixture data, cached by the CDN, with
  no secrets and no calls into the cluster: previews and the showcase. No
  workflow runs with secrets on a fork PR (`pull_request_target` previews
  move off GitHub Actions).

Until the reviewer App exists, local review agents approve as `jomcgi` and
the connector enqueues (bootstrap).

## Delivery plan

Each step is its own PR with live evidence recorded on #7817.

1. This document.
2. Align Substrate with kagent: fork charts and images, preview gates,
   credential provider, egress CA, router and owner entries.
3. Garage (three replicas) and the factory CNPG cluster.
4. Move the Codex OAuth provider out of EmberVM's `internal/`.
5. Chart scaffold with kagent.
6. itsaplan.
7. Credential broker.
8. The homelab repository binding; proves credentials never reach the
   workspace.
9. Connector dispatch.
10. Connector delivery: PRs, review, revision, merge.
11. Reviewer and ruleset.
12. Telemetry for delivery metrics.
13. Install, upgrade and restore runbook.

## Inputs from Joe

- **Codex.** Which ChatGPT account, device-code login enabled, the broker's
  own login, the model, and acceptance of OpenAI's public-repository
  guidance (or an API key instead).
- **GitHub App** `jomcgi-factory` (App ID 5757158) in jomcgi-org: Contents
  and Pull requests read/write, Merge queues read/write [I], Checks, Commit
  statuses and Metadata read; installed on `jomcgi-org/homelab`. 1Password
  item `k8s-homelab/factory-gh-app`, an SSH Key item: its `private key`
  field reaches the Secret as `private-key`, beside text fields `app-id` and
  `installation-id`. Created 2026-11-14.
- **Reviewer.** Decided 2026-11-14: while the factory is bootstrapped, local
  review agents approve its PRs as `jomcgi` (`trustedReviewers`). Afterwards
  the reviewer App `jomcgi-factory-review` (App ID 5758326; checks, merge
  queues, issues and pull requests write, contents and commit statuses
  read) owns review and enqueueing ("Merge authority"). Joe granted issues
  and pull requests write on 2026-11-15
  ([decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-73#comment-474)):
  both for the reviewer's PR comment, which GitHub refuses with issues write
  alone, and pull requests write also for inline findings (HOME-72). Its 1Password item is
  `k8s-homelab/factory-review-gh-app`, an SSH Key item like the author's.
  For now the dispatcher splits enqueueing: the reviewer enqueues Joe's
  PRs and the connector the factory's, since it sees their stops.
- **Third-party Apps.** Remove or reduce Apps with write access that are not
  the factory's (Polylane, the Codex connector, Semgrep, Claude), and remove
  `administration` from BuildBuddy, which posts `pr-checks` until in-cluster
  CI replaces it.
- **1Password, later.** `k8s-homelab/factory-itsaplan` (`BETTER_AUTH_SECRET`,
  `APP_ENCRYPTION_KEY`) and `k8s-homelab/factory-itsaplan-admin` once
  Postgres has off-site backups; until then both are generated in the
  cluster ("itsaplan").
- **itsaplan invites.** The address each person signs in with, invited by
  the break-glass admin before their first OIDC sign-in.
- **Hostnames.** `factory.jomcgi.dev` stays the private entry point and links
  to `factory-plan.jomcgi.dev` (itsaplan) and `factory-agents.jomcgi.dev`
  (kagent).

Everything except live Codex runs, agent pushes and required review can be
built and trialled before these arrive.
