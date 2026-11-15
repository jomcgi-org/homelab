# Factory architecture

The reusable software factory that runs on the factory home cluster
(`projects/factory-cluster/`). This file records the design for steps 2
and 3 of #7817: installing the foundation and delivering the first reviewed
change. Labels: **[V]** verified in upstream source, docs or live state;
**[I]** inferred, to confirm in the PR that depends on it.

## Components and ownership

| Responsibility | Component | Pin |
| --- | --- | --- |
| Work requests, assignment, work feed, run queue | itsaplan Community | v1.10.0 (`ghcr.io/croffasia/itsaplan-{api,web,worker}`), by digest |
| Agent definitions, execution, sessions | kagent | v1.0.1 charts `oci://ghcr.io/kagent-dev/kagent/helm/{kagent-crds,kagent}` |
| Isolated workspaces (actors) | Agent Substrate, kagent-dev fork | v1.0.1 |
| Coding harness | Codex CLI | 0.161.0, as the kagent harness image pins it |
| Repository, PR, CI, merge queue | GitHub | |
| Provider and GitHub credentials | `projects/factory/broker` | this repo |
| Relationships between itsaplan, kagent and GitHub | `projects/factory/connector` | this repo |

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
    starts only when the issue's delegate changes, with a fixed prompt, and
    an agent cannot create a run any other way without Pro schedules.
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

## Connector

Go, built with rules_go and apko, one replica with `Recreate` and a Postgres
advisory lock. Each takeover increments a writer epoch, and every external
side effect checks it first. The epoch check runs on the lock's own
connection, so a lost session also fails it. kagent guarantees one non-quiescent task per
session and one execution per actor, which keeps one writer per worktree.

**Data**, in its own database: repository bindings, work items (issue,
binding, branch `factory/<KEY>`, kagent session and generation, state,
review rounds), runs (itsaplan's, and the connector's own review rounds),
delivery attempts (unique per run and sequence number, A2A message ID,
kagent task, error class, evidence URI, and the itsaplan attempt as
`run_attempt`, since engineering retries add sends within one itsaplan
attempt), pull requests (head SHA, merge-queue
state), review events (unique GitHub node ID, commit SHA, disposition),
cancellations and an outbox. Full transcripts and CI logs go to the
`factory-evidence` bucket. Migrations are timestamped SQL files, named like
the monolith's, embedded in the binary and applied at startup under an
advisory lock. Database tests start PostgreSQL 16 from `@postgres_test`, as
the monolith's pytest plugin does.

**Run transcripts.** Native Codex text arrives in task artifacts, which can
grow without an assistant history message. The connector journals events
and per-artifact byte cursors in `task_transcript` before posting to itsaplan.
Journal JSON is stored as `bytea`, retaining U+0000 and preserving existing
journals through a conversion migration. itsaplan's JSONB trace storage and
the connector's JSONB result outbox cannot store NUL, so delivered text uses
U+FFFD in its place. Replacement happens after event chunking and does not
change event counts; artifact cursors still count and validate the raw bytes.
Each journal belongs to one run attempt and kagent task, with a fixed base
offset after any earlier engineering retry. Artifact message IDs hash the
full task and artifact IDs; new snapshots append only unseen text. END is
recorded once when the task becomes quiescent because task snapshots omit
the artifact stream's `LastChunk` flag. History retains its existing event
rendering, including offsets acknowledged before the journal was introduced.

**Why.** Re-rendering a growing artifact changes event boundaries and moves
acknowledged offsets. Persisting events and cursors together lets a restart
replay the exact batch after a lost acknowledgement, even if the next task
snapshot has more text. A missing artifact or a changed or shortened prefix
fails the poll without advancing the transcript or reporting completion
(#8017).

**Rollback transition (#8017).** Chart 0.27.11 shipped the journal with
`dispatch.Config.ArtifactTranscripts` false: new journals held history events
only, and an existing artifact journal recorded its admission and always
resumed, including pending events whose acknowledgement was lost. Chart
0.29.10 ran that way in production, and HOME-102 sets it true in
`cmd/connector/main.go`.

To roll back, lower the deploy pin to 0.29.10 or any journal-aware release
with admission off. It resumes active artifact journals and keeps new ones
history-only. A direct rollback to a release before 0.27.11 is unsupported:
it would misread the artifact offsets already acknowledged.

**Dispatch.** Runs come from itsaplan assignment, mentions and delegation;
none of these is a Pro feature (status-triggered schedules are Pro and are
not used). Claim runs per agent key. A claimed run with an open attempt
is a transport redelivery: find the kagent task by message ID and attach,
never send again while kagent could still accept it. Agents have no itsaplan
tools, and itsaplan's run prompt only names the issue, so a send that opens
a session quotes the issue's title and description, read with the
coordinator key and recorded with the attempt so resends stay identical.
HOME-1, the first live run, ended "blocked" without this. A new send uses message
ID `itsaplan-run/<runId>/<decisionId>`, where the decision ID is the
connector's record of why it sends (initial, revision or engineering retry).
The attempt is recorded, with the run's state, before the send. The
connector never sets a task ID, so every send opens a task. A definitive
not-accepted response leaves the attempt ready to retry, spaced by 15 s;
after 20 refusals it tries once per dispatch window three more times. The
ready state is persisted only after that response. A crash before the write
leaves ambiguity and cannot resend.

After any ambiguous response, the connector only reads every task state and
page looking for the original message ID. A positive match attaches the
original task, including a task that finished or is waiting for input. It
never sends that message again or replaces its context merely because the
task or session is missing. kagent may remove an idle session's creation
receipt; resending the same initial message after a long outage could then
create another actor. Its NodeGone deletion path can also hide an actor
whose machine has not physically stopped, so even a complete empty listing
does not prove the original execution ceased.

After a dispatch window with no positive match, the connector keeps the
original attempt and concurrency slot, records an operator-visible hold,
and announces it once on the issue. Missing, malformed or incomplete reads
retain that hold; later positive evidence resumes the original task. A
request that truly never arrived may also remain held. Recovery requires
operator verification that the original runtime is fenced; empty task
lists and elapsed time cannot authorize retry or cancellation confirmation.

Task scans retain page coverage metadata: a negative scan must observe a
stable total and exactly that many distinct task IDs. Counts and page rows
use separate queries, so concurrent insertion or deletion can make a scan
inconsistent. Such a scan may still positively identify the original task.
Every non-matching task must expose its opening user input: a first
terminal or waiting save may remain publicly SUBMITTED with hidden history
until native cleanup finishes. Null/partial responses, failed reads,
repeated cursors and scan timeouts remain uncertainty. None of these
negative scans releases an unknown send.

This guarantee is specific to an unknown send: it never causes another send
or replacement from absence alone. A positively observed original task
continues through the existing completion and lifecycle handling. That does
not prove physical quiescence for every published terminal state: kagent
can publish saved outcomes or synthetic failure after an inferred crash,
and Substrate's NodeGone cleanup can succeed without an independent kill
acknowledgment. That platform fencing gap is tracked separately from send
reconciliation; missing-task recovery must not add a new cessation inference.

The dispatch window covers the 60 s call timeout, the two-minute reservation
and 30 s of skew; configuration refuses anything under 210 s. The repository
and branch come only from configuration.

Before the first send, the work item keeps an immutable caller witness:
projected token issuer, ServiceAccount UID, the controller's configured
authority, and the A2A and gRPC endpoints. Each observation/action sequence
pins one projected token for both protocols and checks that witness before
sending, cancelling or relying on session absence. Token rotation preserves
the witness; a recreated ServiceAccount, changed authority or changed
endpoint holds the original attempt without spending a retry. Legacy rows
without a witness need positive ownership from a readable session whose
creator matches the current principal; an unknown first send must first be
found by its exact message ID. A new unsent retry cannot establish ownership
for older work that may still execute: all uncertain historical sends must
also have readable ownership and quiescent tasks. Missing legacy evidence stays held
until the original identity or positive ownership can be restored. Tokens
are never persisted in the witness. The controller authority and kept
ServiceAccount remain immutable operational requirements (finding 15).

A work item keeps one kagent session whichever agent key delivers its run.
When a run for an existing work item arrives under a different agent, the
connector bumps the work item's epoch, which fences attempts sent under the
previous one. It confirms the mapped session has no running task, cancelling
it first if needed, and continues in that session. The work item records
the session's kagent Agent, and every send to the session goes to that
Agent, whichever itsaplan agent key delivers the run. A new session is
created only by the replacement path below.

**Heartbeat** every 60 s, from a loop separate from dispatch, so a slow
kagent call cannot let a lease lapse. On 404, read the run through the
runner queue with the coordinator's key: canceled starts cancellation; a
higher attempt means this attempt is stale and stops writing; a run itsaplan
finished without this attempt's result has its kagent task canceled before
it closes. The connector looks each run up once after claiming it. Once the
queue has shown a run, a lookup that no longer shows it counts as canceled,
since moving its issue is the v1.10.0 cancel path and hides it. Before that
sighting an empty lookup changes nothing. Progress is
posted as itsaplan run events with a persisted offset.

**Completion.** The connector opens or updates the one PR for the work
item from the pushed branch. A completed turn records its attempt on the
work item in the same transaction as the run's result; delivery then
compares the branch with the base and, when it has commits, finds the
branch's PR or opens it, titled with the head commit's subject. The body
links the issue, run, session and delivery attempt of the latest turn. The
connector always looks the branch up before opening a PR and again after a
failed open, and GitHub holds one open PR per head, so a restart or a lost
response never opens a second. The connector never reopens a PR: a
closed, unmerged one, or one against another base branch, stops delivery
for the work item (below). A turn without commits opens nothing and says
so on the issue. One issue maps to one merged PR: a turn that pushes to
the branch after the merge is not delivered, and the issue says follow-up
work needs a new issue. A run result means
the turn finished; the issue becomes Done only from the observed merge. Engineering retries are
connector decisions bounded per work item; the retry and the attempt it
sends commit together. Credential and quota failures finish the external
attempt with persisted failure evidence and keep the run waiting until an
operator unblocks its grant. The class comes from whole identifiers in the
status text (`codex_login_required`, `unauthorized`, `usage_limit_reached`, an HTTP
401 or 429 status). A block covers every work item on the same kagent
Agent, the scope of its grant, and is announced once on the issue that hit
it. Issue comments name only the failure class: the status text is model
or provider output, so it stays in the logs, the attempt and the itsaplan
result. Only an operator clears it, with `connector unblock
kagent:<namespace>/<agent>` after fixing the grant: itsaplan v1.10.0 runs
carry no requester identity, and a mention can come from an agent. Failed
runs and runs claimed meanwhile wait without holding a concurrency slot,
keeping their itsaplan leases alive. After unblock, failed runs consume
an engineering retry and send a fresh deterministic retry message in the same
session within the agent key's concurrency. Polling while blocked spends no
retries. An exhausted run reports the existing engineering-retries-exhausted
failure only after unblock. Running or ambiguous tasks keep their existing
reconciliation path. The reported
usage is the last model call's, read from `kagent.dev/a2a/usage`.

**Review.** Poll owned PRs every 60 s (no public webhook): GraphQL for the
PR, its reviews and review threads, REST for check runs and statuses on the
head. Events that ask for changes are a `CHANGES_REQUESTED` review, or a
failed run of a configured check or status. Each is recorded once by its
GitHub node ID. It acts only if its commit is the current head and its
author is trusted by the binding: a trusted reviewer login, a check run
from the check's App ID, or a status from the check's creator login.
Otherwise it is recorded `stale_head` or `untrusted`. A round acts once per
head, kind and source (reviewer or check name); a later event with the same
key, such as a re-run failing again on an unchanged head, is `duplicate`.
Acting events start one review round with their text and the unresolved
threads, and the start is announced on the issue. The repository is
public, so only thread comments by trusted reviewer logins reach the
prompt or a replacement's context; the rest are dropped and the text says
how many. Thread paths are replaced with a fixed label, since the change
author chooses them, even on threads with trusted comments. A reviewer
App's comments reach the agent only if its bot login
is listed in `trustedReviewers`. Rounds are bounded per
work item (`maxReviewRounds`, default 3); past the bound events are
`round_limit` and the PR waits for a person. Events are read only while
the work item is in review, not while a turn runs.

An open PR outside the merge queue whose mergeability is `CONFLICTING`
starts a rebase round on the next review poll, before the enqueue gate and
without waiting for checks to finish. `UNKNOWN` waits for the next poll.
The prompt asks the agent to rebase on the binding's `origin/<base>`, resolve
the conflicts, rerun affected tests and push. If checks or reviews also ask
for changes on that poll, their feedback goes into that same round. A
synthetic conflict event records the head once, atomically with the round
and its issue announcement; repeated polls and connector restarts cannot
start another rebase on that head. Holds and stopped work items block it
like any other revision trigger.
Once that conflict event is consumed, independently new check failures or
trusted review feedback can still start a review round within `maxReviewRounds`.
Its prompt includes the remaining conflict, but the rebase count does not
increase again for the same head.

Rebase rounds use a separate per-binding `maxRebaseRounds` bound (default 3,
0 disables them), persisted as `work_item.rebase_rounds`. They do not spend
`maxReviewRounds`, including when the prompt also carries review feedback.
Past the bound, one issue comment says the PR conflicts and waits for a
person. `factory_connector_rebase_rounds_total{outcome}` counts starts,
idle rounds and work items reaching the bound (`started`, `idle`, `limited`).
No conflicting head is enqueued. A rebased head needs fresh CI and independent
review before it may enter the queue.

**Why.** Main moving is not a review failure. Before
[HOME-71](https://factory-plan.jomcgi.dev/factory/issue/HOME-71), only queue
ejections carried conflicts back to the agent: PR #8058 repeatedly failed
enqueue with a conflict while its issue stayed silent. Detecting conflicts
during review repairs that case within a separate bound.

The reviewer marks a check that failed only because the PR is over its size
budget with a service-only `split-only` marker. When that check, from the
binding's reviewer App, is the only acting event on the head, it starts a
split round instead of a review round. The prompt asks the agent to reduce
the PR to one coherent part within the budget and to name what it removed in
the commit message. Split rounds use a separate per-binding `maxSplitRounds`
bound (default 1, 0 disables them), persisted as `work_item.split_rounds`.
Past the bound, one issue comment says the PR is over the budget and waits
for a person. `factory_connector_split_rounds_total{outcome}` counts
`started`, `idle` and `limited`. A split request that arrives with other
failures on the head starts the round they would start. The marker changes
no check conclusion, and the reviewer strips its prefix from all PR and
agent text, as it does for the human-gate marker.

**Why.** A size failure is not a review finding, and no review ran.
PR #8062 spent the first of its three review rounds on one
([HOME-107](https://factory-plan.jomcgi.dev/factory/issue/HOME-107)).

Every round prompt says the reviewer reads commit messages, not the agent's
reply. For each finding it does not fix, the agent adds a "Review response"
to the commit it pushes: the finding ID, why, and evidence the reviewer can
check (file:line, a test, a CI run or an issue). With no code change it
records the responses in an empty commit (`git commit --allow-empty`), so
the reviewer reviews a new head.

**Why.** `REVIEW.md` "Clearing a finding without a fix" lets an author
justify a medium or withdraw a wrong finding with evidence, but the agent's
reply reached no one. The reviewer reads PR comments only from
`trustedCommenters` and the PR body, which the connector writes without the
change's intent. On PR #8037 the reviewer repeated a finding verbatim after
the agent's reply showed the read was idempotent. Commit messages are already
public, so this channel publishes nothing new
([HOME-107](https://factory-plan.jomcgi.dev/factory/issue/HOME-107)).

A review round is a connector-initiated continuation, not an itsaplan
mention-run: itsaplan v1.10.0 starts no run for an agent's comment
(finding 13). The round is a connector run (negative id, message IDs
`factory-review/<n>/<decision>`) that dispatch sends like any other
continuation in the mapped session, with no lease, transcript or result in
itsaplan; its record lives in the connector database and on the issue. A
round holds a slot of its agent key's concurrency and waits for one. A
failed round returns the work item to review.

A round records the head it was asked to change. If it closes with the PR
still at that head, the agent pushed nothing. Consumed events cannot start
another round; independently new feedback remains eligible within the review
bound. Once every required check and
the reviewer check has finished on that head and the gate is still closed,
the connector says so on the issue once per round. A still-conflicting head
gets this idle-round comment without waiting for checks to finish. The PR
waits for a person; a new commit restarts CI and review, and the comment says how many
rounds remain. itsaplan comments carry no idempotency key, so this one is
sent at most once: `outbox.send_started_at` is set before the send, a send
whose outcome is unknown is abandoned rather than repeated, and only a send
that provably created nothing (no connection, or a 4xx refusal such as a 401
during a key rotation) is retried under the outbox's usual bounds.
`factory_connector_idle_review_rounds_total` counts these. Before this,
HOME-15 and HOME-18 waited silently for eight hours after rounds whose
agent could not read the CI failure.

**Stopping delivery.** A round has no itsaplan run to cancel, and its
kagent session belongs to the connector, so the connector watches for the
signals a person gives. It reads the issue before a round starts, every
heartbeat interval while one runs, before it opens or updates the PR, and
on every PR poll, so before any enqueue and any merge into Done. Delivery
stops if the issue left the binding's project or sits in a `completed` or
`canceled` column. A closed PR stops it too, whether the PR poll sees it
(including a PR closed while queued) or the heartbeat of a running turn.
`connector cancel <ISSUE> [--by NAME]` stops it from the operator's shell
and records who did. A stopped work item gets no PR updates or rounds.
Every running turn on it, itsaplan's or a round, gets a pending
cancellation and is reported failed, with the reason, only once kagent
confirms the task stopped. Its open PR is still polled: a queued PR is
dequeued with GraphQL `dequeuePullRequest`, and a merge is recorded and
commented but never moves the issue to Done. State writes that would
resume a stopped item (a round start, enqueue, ejection, merge) are
guarded, so a stop that races them stands; an enqueue that lost the race
is dequeued. The stop is announced on the issue. A new itsaplan run on the
issue resumes delivery; a closed PR must be reopened by a person first.

So a person cancels a round by moving the issue to a canceled column (or
another project), by closing the PR, or with `connector cancel`; it
reaches kagent within a heartbeat interval.

A missing accepted task or a session observed crashed, deleting, deleted,
missing, or unrecoverable enters an explicit fencing hold. The connector does
not revert or replace it automatically: controller absence and READY after a
revert do not prove the old physical writer stopped. A positive original task
clears a missing-task hold; a lifecycle hold remains sticky until attested,
even if the task returns or the session later reports READY. The attempt and
concurrency slot remain occupied, including an unsent continuation whose
previous session is uncertain.

An operator with the connector's database access can inspect and recover it:

```sh
connector recovery inspect HL-123
connector recovery confirm-fenced HOLD_ID --fingerprint HASH --action retry \
  --by OPERATOR --evidence 'fencing evidence or incident reference' \
  --confirm-all-copies-fenced
```

Inspection includes the immutable caller witness, target, every uncertain
historical attempt, and the earliest confirmation time. Fence every possible
execution copy, including copies under historical principals that are no
longer visible, and quiesce retired or in-flight dispatchers. Keep them fenced;
recovery never authorizes their restart. Wait for the dispatch window before
confirming. This command records an operator attestation, not a code-verified
kill. Use `--action cancel` to stop the current target without replacing it.

The connector revalidates the complete fingerprint and current writer epoch
before consuming that decision in one transaction. Changed authority, a new
predecessor, positive reconciliation or stronger lifecycle evidence invalidates
obsolete approval. Historical attempts are evidence only, never restarted.
A bound caller cannot be changed by confirmation. Legacy unbound work can
adopt the current caller only after attestation covers every possible prior
copy. Retry spends the existing bounded engineering budget and records one
fresh-session attempt with issue, structural PR and branch data, and trusted
thread context; an untrusted PR title is omitted. Fresh authoritative issue text is
required before consuming retry attestation, even after a terminal lookup error
or prolonged 401/403. Failed reads retain the attempt, slot and attestation.
Pending cancellation or a stopped work item takes precedence over retry,
without requiring replacement-context reads. Send admission separately checks
current stop, cancellation, run attempt and work epoch under the writer fence.
Attestations and their consumed/invalidated states remain in the audit trail.

**Merge.** When the required checks and the trusted reviewer succeed on the
current head, enqueue with GraphQL `enqueuePullRequest` and
`expectedHeadOid` set to that head. The reviewer is the binding's reviewer
check, or without one a trusted reviewer's approval of the head; a trusted
reviewer's standing change request blocks either way. A PR already in the
queue is recorded as enqueued, never enqueued twice. A PR against another
base branch is never enqueued. When the merge is observed, the connector comments the issue, run, session, PR and merge
links on the issue and moves it to the project's first `completed` column
(or `doneColumn`); a move itsaplan refuses, such as a full column at a hard
WIP limit, is reported on the issue and retried with backoff. A move the
connector gives up on is also reported on the issue, unless a person
canceled the issue since the merge.

A PR that leaves the queue unmerged, after a second read confirms it did
not merge, starts a revision in the mapped session, counting against
`maxReviewRounds`.
The connector saves the queue entry's synthetic commit while queued, then
reads its failing checks and statuses, matched against configured names,
App IDs and status creators. The prompt uses the configured names and fixed
failure states. Check links are built from the
repository and numeric check ID; statuses link to the merge-group commit's
checks page. CI-provided URLs, summaries and descriptions are omitted. If the
queue entry disappeared before its commit was observed, the prompt says the
evidence is unavailable.
GitHub's `CONFLICTING` mergeability asks the agent to rebase on the bound
`origin/<base>` and resolve. This stays one ejection round against the review
bound and consumes the head's conflict event, so an unchanged head cannot
start another rebase round on a later review poll. If the review bound is
already exhausted, the ejection fences the head's conflict trigger as
`round_limit` in the same transaction and waits for a person, even if GitHub
reports `UNKNOWN` or `MERGEABLE` at that time. A later `CONFLICTING` result
on that head cannot switch to the separate rebase budget. A new head remains
eligible for that budget. A CI infrastructure flake
may be repaired by rebasing and re-pushing the same change; the new head needs
the usual checks and independent review.

The ejection and round start commit together and are announced on the issue.
An ejected head is never re-enqueued. A new head
returns to ordinary review without spending a round on the previous entry.
Before starting a revision the connector reads the PR head's reviewer check
for its human-gate marker, even when the synthetic commit was not observed,
then refreshes the issue and PR stops. A `human` label observed while queued
stops delivery durably; removing it does not resume the work item. Other
human holds and the round bound leave the work item in review for a person.
On every review poll, a hold label or the current head's reviewer human-gate
marker blocks all revision triggers, including newly failed checks, statuses
and change requests. Pending events stay unconsumed until the hold clears.
Ejection reads are recorded in `delivery_read` before each external call,
with a stable message ID, writer epoch and attempt count. Retries share the
read ID and always fetch fresh state, including after a writer takeover.
Deployment health is a separate record. After the handover in "Merge
authority" (#7919) the connector still enqueues the factory's PRs, since it
sees their stops, and GitHub's required
`factory review` check gates them.

**Cancellation** sends A2A `CancelTask` and stays pending until kagent
reports the task in a terminal state. A cancellation never sends a message.
For an attempt whose task is unknown, it waits a dispatch window after the
last send. It then finds the task in any state whose history holds the
attempt's message ID, looking in the attempt's session or, for a first
send, across all of the original caller's sessions. A terminal task confirms
cancellation; a running or waiting task is cancelled. A complete scan with
no match keeps cancellation pending and records the recovery hold described
above. A never-sent or definitively unaccepted attempt with no prior-session
uncertainty can confirm cancellation without a task. Otherwise the explicit
operator fencing attestation above is required. A run that itsaplan ended closes once its
cancellation is confirmed, whatever started that cancellation.

**Deployment.** `templates/connector*.yaml` render with `connector.enabled`,
which needs `itsaplan.enabled`, `broker.enabled` and kagent's
`config.server.auth.serviceAccount.authority`. That authority is part of the
connector's kagent principal (finding 15), so it never changes once set.
The chart passes this same authority to the connector in
`FACTORY_CONNECTOR_KAGENT_AUTHORITY`, including existing-controller mode.

- **Pod.** Deployment `factory/factory-connector`, one replica, `Recreate`,
  wave 1, the image by digest with the GHCR pull secret. Its ServiceAccount
  `factory-connector` is kept (finding 6) and mounts no API token: the
  connector calls no Kubernetes API.
- **Configuration.** Each binding takes owner, repository, base branch,
  namespace and kagent Agent from `repositories`, and its itsaplan project,
  agent keys and review policy from `connector.bindings.<name>`. The
  connector's agent keys are itsaplan bootstrap agents: `homelab-codex`
  claims runs and `factory-coordinator` reads and comments. Their keys are
  mounted, as optional, from the bootstrap's
  `factory-itsaplan-agent-<username>` Secrets. The bootstrap is a PostSync
  hook that runs only once the connector's Deployment is healthy, so on a
  fresh install or a restore the connector serves `/healthz` first and waits
  for the key files with backoff, logging which are missing. It reads a key
  on every call, so a rotated key needs no restart. A call sent with the old
  key during a rotation, or before the kubelet refreshes the projected file,
  gets a 401 or 403, which is never final: claims, heartbeats and events
  try again next pass, and an outbox item (result, comment, Done move)
  backs off as for any other failure. After 12 tries, about 27 minutes, the
  connector abandons the item as it would a refused one and logs that
  itsaplan keeps refusing its key. The Done move reads the issue first and
  waits while it cannot. Coordinator lookups that hold a run's slot have a
  30-minute bound, kept in memory per attempt: the initial-session path then
  proceeds without issue text, and a dispatched run whose attempt itsaplan
  no longer accepts closes. Fenced recovery requires authoritative issue text
  before creating a replacement and waits beyond this bound. Lookups that hold
  no slot, the issue check before PR updates, rounds and the merge queue and
  a stale run's lookup, retry
  until the key works. `factory_connector_credential_refusals_total` counts
  itsaplan 401 and 403 answers by key role and kind. A startup probe allows
  five minutes before liveness counts. The database URL
  comes from `factory-connector-db`, which `factory-db-secrets` declares and
  `factory-db-setup` fills. Issue links default to itsaplan's
  `/issue/{issueId}` redirect.
- **Tokens.** Two projected ServiceAccount tokens, one per audience:
  `kagent-invocation` for the controller's A2A gateway and SessionService
  (`kagent-controller.kagent.svc:8083`), `factory-broker` for the broker's
  admin port. The chart adds `system:serviceaccount:factory:factory-connector`
  to the broker's `connectorSubjects`. kagent needs no grant for the
  connector (finding 15); the controller's ingress policy is the boundary.
- **Delivery** turns on with `broker.githubApp.enabled`: only then does the
  connector get the broker URL, its token and egress to `api.github.com`.
  Without the App it claims and dispatches runs and reports their results,
  but opens no pull requests.
- **Network.** Egress to itsaplan's api (3000), the kagent controller (8083),
  the broker's admin port (8081), `factory-db` (`connector.database.endpoints`,
  5432) and `api.github.com:443`. Each peer admits it by
  `networkPolicy.connector`'s labels. Nothing connects to it: metrics on
  9090 have no scraper on this cluster yet. It writes no evidence to
  `factory-evidence` yet, so it has no Garage egress.

## Credential broker

`projects/factory/broker`, one replica with `Recreate`. It reuses the Codex
OAuth code in `projects/shared/go/tokenbroker`, with the grant Secret prefix
(`factory-broker-grant-`), metric prefix (`factory_broker_`) and refresh
window configured per consumer; EmberVM keeps its defaults. No Lease: grant
writes are newer-only and a `refresh_token_reused` answer rereads the Secret
and retries once, so a brief overlap during a rollout is safe.

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
  (401); `codex_login_required` (401, only for an empty grant or a refresh
  token the provider answered `invalid_grant`); `codex_quota_exhausted`
  (429 with `resets_at` and `Retry-After`, from the `x-codex-*` headers or a
  `usage_limit_reached` body; a current exhausted window blocks before the
  backend is called); `codex_grant_unavailable` (503, transient: a failed
  refresh, a missing grant Secret, or the backend refusing a freshly
  refreshed token); `relay_busy` (503). A backend 401 replaces the token
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
  `app.kubernetes.io/managed-by=factory-broker`.
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
  (reading up to 1,000 comments a page at a time and stopping at the first
  match, so the reviewer makes one request), creates a comment on a pull
  request (an issue number is refused), and edits a comment on a pull
  request that the bot login wrote. It serves
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
  second port serves the admin endpoints, connector tokens and metrics, and
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

| Who | How |
| --- | --- |
| Humans to itsaplan | Edge OIDC, then itsaplan's own OIDC client `factory-itsaplan` in Authentik; invite-only registration; a break-glass admin with a generated password |
| Humans to kagent | Edge OIDC with the access token forwarded; kagent in trusted-proxy mode. Sign-in alone grants nothing: the edge admits only the Authentik operators group, and kagent session ownership limits each user to their own sessions |
| Connector to kagent | Projected ServiceAccount token (audience `kagent-invocation`) of the kept ServiceAccount `factory/factory-connector`, reviewed by kagent with TokenReview; principal `kubernetes:<authority>` and the ServiceAccount UID (finding 15) |
| Connector to itsaplan | A coordinator external agent for comments, issue reads, run lookups and moving merged issues to Done (`work_items read` and `edit`, `ai_agents read` in each bound project); the coding agent's key claims runs |
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
  (`factory_reviewer_comment_writes_total`). It edits only a comment its bot
  login wrote, and its own comment never reaches the agent.
  Check runs are its only state: the
  in-progress run names its attempt and kagent task, and a completed one
  keeps its blocking findings, human gate and kagent session in a hidden
  block, at most 48,000 characters, for the next head. Duplicate ready PRs
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
  run `gpt-6.1-terra`) in
  `reviewer.bindings.<name>.namespace`, default `<coding namespace>-review`.
  The shared binding template creates its ModelConfig with an actor JWT,
  Harness, WorkerPool, git EgressPolicy and credential-provider RoleBinding.
  The namespace cannot overlap any coding or review binding: both Apps
  write `github-git`. The broker admits the review atespaces to its relay,
  mounts the separate reviewer App's key files from 1Password and writes
  their read-only git tokens. Network policy permits the reviewer service
  only to the broker's admin port (8081), kagent A2A (8083) and
  `api.github.com:443`, and admits it at both peers.
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
  `findings[{id, severity, path, line, title, detail, defer, earlier}]`,
  `previous[{id, status, note}]`, IDs `<path>:<rule>`), and the service
  decides: success only when no critical, high or medium finding blocks.
  An attempt without a usable report (unparseable output, an unknown
  severity, a failed task such as a quota or login error, a rejected send or
  a timeout) is retried on the same head after `retryBackoff` (5m); the
  third such attempt fails the check. A later head's prompt carries the
  previous blocking and `human-gate` findings and the interdiff; each comes
  back `fixed`,
  `justified` (a medium with a note, or any severity with a note starting
  `withdrawn`) or `open`, and one left out stays open. One reported again
  keeps the higher of its two severities unless it is fixed or withdrawn. A
  `human-gate` finding not fixed or withdrawn holds the later head too.
  The check's State also keeps up to 20 findings earlier reviews cleared
  (fixed, justified or withdrawn, with notes). The prompt lets the agent
  raise a fixed one again whenever it is present (a regression), and a
  justified or withdrawn one when a change since the previous head
  affects it or when it is present at high or critical severity. It asks
  the agent to check each again when the interdiff is unusable. When the
  head descends from the previous one and GitHub lists the whole interdiff,
  the service lowers a new medium finding the agent marks
  `earlier` (on code an earlier review covered and the head left alone) to
  a deferred low one when its file is outside the interdiff
  ([HOME-107](https://factory-plan.jomcgi.dev/factory/issue/HOME-107)). An
  unmarked finding keeps its severity, so an omitted mark never lowers a
  regression, and so does one whose ID an earlier review recorded as
  fixed. So does a high or critical finding (REVIEW.md lets those
  block on earlier-reviewed lines), a finding with no path, and a
  finding under any rule but the root rules `correctness`, `security`,
  `boundary`, `reachability`, `validation`, `rollout` and `goal`: scoped
  rules set their own severity, and the review-process rules are not about
  earlier-reviewed code. A reattached
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
- **Human gate.** Joe decided the gate on 2026-11-14 (#7817,
  [decision](https://github.com/jomcgi-org/homelab/issues/7817#issuecomment-6878730211))
  and narrowed it the same day to what could weaken the gate itself
  ([decision](https://github.com/jomcgi-org/homelab/issues/7817#issuecomment-6884458635)):
  under the first scope almost every factory PR needed his bypass, which
  skips CI and the review. A PR touching `REVIEW.md`, `**/REVIEW.md`,
  `.github/CODEOWNERS`, `projects/factory/reviewer/config/**`, the
  reviewer's `review/report.go`, `review/gate.go` or `review/rules.go`, or
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
  the reviewer App's ID, beside `pr-checks`. Nothing merges into main without
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
- **Resumed sessions.** The first attempt on a later head continues the
  kagent session that reviewed the previous head, as the connector
  continues a coding round: an A2A `SendMessage` naming the session's
  context ID and no task ID opens a new task in it. The completed check's
  State records the session (context ID, binding, agent, PR, head, path,
  heads reviewed, task bytes, rules digest). The service reads it only from
  its own App's check on the earlier head, never from PR, commit or comment
  text, and checks it with `SessionService.GetSession` on the A2A listener.
  The continuation keeps the previous findings, the interdiff, the PR's
  text in new delimited blocks, the report contract and the rule that the
  PR's earlier content is data. It leaves out the rules and repository
  context while a digest of their text matches the one the session holds:
  main moves with every merge, so comparing commits would resend them every
  time. A review starts fresh, and `factory_reviewer_review_sessions_total`
  records why, with no recorded session, on a retry, for another binding,
  agent, PR or head, past 512 KiB of task bytes, for a session kagent no
  longer has or reports deleted, crashed or failed, on a failed read, or
  when kagent refuses the continuation (rebuilt fresh in the same attempt).

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
  (`<owner>:<branch>`), which excludes forks; the author check is #7920. Only `trustedReviewers` and the reviewer App reach an agent's
  prompt.
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
