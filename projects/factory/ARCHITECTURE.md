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
   connector, Substrate's `atenet-egress` (actors' calls, which carry a
   verified actor JWT) and the controller itself (its own `kagent-api`
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
12. **kagent reports usage per model call.** [V] Each call's usage sits in
    metadata key `kagent.dev/a2a/usage` (`promptTokenCount`,
    `candidatesTokenCount`).
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
  generated password, still behind the edge's sign-in.
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
connector never sets a task ID, so every send opens a task and kagent
accepts it once (finding 9). After an ambiguous response the connector
sends the same message again, at most every 15 s: kagent answers with the
task holding it, refuses while the first copy's reservation is in flight,
or takes it when the first copy never arrived. After 20 sends, the
connector sends only once per dispatch window, three more times, since a
late save of the last send would be found that way. The attempt fails as
an engineering failure a full window after its final send, when kagent can
no longer commit any of them. The window covers the 60 s call timeout, the
two-minute reservation and 30 s of skew; configuration refuses anything
under 210 s. The repository and branch
come only from configuration.

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
how many. A reviewer App's comments reach the agent only if its bot login
is listed in `trustedReviewers`. Rounds are bounded per
work item (`maxReviewRounds`, default 3); past the bound events are
`round_limit` and the PR waits for a person. Events are read only while
the work item is in review, not while a turn runs.

A review round is a connector-initiated continuation, not an itsaplan
mention-run: itsaplan v1.10.0 starts no run for an agent's comment
(finding 13). The round is a connector run (negative id, message IDs
`factory-review/<n>/<decision>`) that dispatch sends like any other
continuation in the mapped session, with no lease, transcript or result in
itsaplan; its record lives in the connector database and on the issue. A
round holds a slot of its agent key's concurrency and waits for one. A
failed round returns the work item to review.

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

A session whose runtime crashed is reverted with kagent `RevertSession`.
Before a continuation the connector checks the session; while a turn runs
it checks every heartbeat interval, since a crashed runtime does not finish
its task. A revert fails the interrupted turn, so its input is sent again
in the same session (`resend-<seq>`, at most twice per run) without using
the engineering-retry budget. A session kagent no longer has (`NOT_FOUND`)
or tells the client to replace (`FAILED_PRECONDITION`) is replaced: the new
session's first message carries context built from the issue, the PR, the
branch head and the unresolved review threads, recorded with the attempt so
every resend is identical, and the replacement is announced.

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
canceled the issue since the merge. A PR that
leaves the queue unmerged, after a second read confirms it did not merge,
returns the work item to review, the ejection is announced, and that head
is not enqueued again; a new push or review moves it on. Deployment health is a separate
record. Under the planned handover in "Merge authority" (#7919), the reviewer App enqueues instead.

**Cancellation** sends A2A `CancelTask` and stays pending until kagent
reports the task in a terminal state. A cancellation never sends a message.
For an attempt whose task is unknown, it waits a dispatch window after the
last send. It then cancels the running task whose history holds the
attempt's message ID, looking in the attempt's session or, for a first
send, in the connector's sessions that no work item maps; with none, it
confirms. A run that itsaplan ended closes once its cancellation is
confirmed, whatever started that cancellation.

**Deployment.** `templates/connector*.yaml` render with `connector.enabled`,
which needs `itsaplan.enabled`, `broker.enabled` and kagent's
`config.server.auth.serviceAccount.authority`. That authority is part of the
connector's kagent principal (finding 15), so it never changes once set.

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
  30-minute bound, kept in memory per attempt: a new or replacement session
  then starts without its issue, and a dispatched run whose attempt itsaplan
  no longer accepts closes. Lookups that hold no slot, the issue check before
  PR updates, rounds and the merge queue and a stale run's lookup, retry
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
  requests in flight, and an upstream stream silent for 5 minutes is cut.
  Quota observations are committed off the request path, latest only.
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
- **Listeners.** The relay port admits only Substrate's egress gateway; a
  second port serves the admin endpoints, connector tokens and metrics, and
  admits only the connector, so no actor can reach them. Operators use a
  port-forward. Cilium L7 rules could split one port instead, but Hubble
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
  outcome, and the shared quota gauges. Labels come only from fixed sets or
  configuration.

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

**Status (2026-11-14): planned.** Today local review agents approve as
`jomcgi`, the connector enqueues with the factory App, and ruleset 9180009
requires only `pr-checks` and the merge queue. The work is #7919 (reviewer
App, service and required check), #7920 (connector authorship check), #7921
(third-party Apps), #7922 (in-cluster CI and registry) and #7923 (fork PRs
and public previews).

- **Author.** `jomcgi-factory` pushes branches and opens PRs: contents and
  pull requests write, checks and statuses read. It loses merge queue write
  once the reviewer takes over enqueueing. The agent's token is contents
  write on its own repository only. Neither can post the review check.
- **Reviewer.** A separate App, `jomcgi-factory-review`, with checks write,
  merge queues write, and contents and pull requests read. A kagent reviewer
  agent, in its own binding, reviews the exact head against
  `docs/agents/review.md`. It posts the `factory review` check run and
  enqueues when the check passes and required CI is green. It never posts a
  PR approval, so it can never count toward an approval or code-owner rule.
- **GitHub enforces it.** Ruleset 9180009 will require `factory review`, bound to
  the reviewer App's ID, beside `pr-checks`. Nothing merges into main without
  a passing review on that head, including PRs from Joe, interactive
  sessions, Dependabot/Renovate and other third-party Apps such as Polylane.
  Third-party Apps may open PRs but merge only through this gate. Joe keeps a
  ruleset bypass for emergencies; nothing else does.
- **Trigger.** kagent v1.0.1 has no trigger resources (no webhook, cron or
  event types); agents run when sent an A2A task. A small deterministic
  `factory-reviewer` Deployment, separate from the connector, with its own
  ServiceAccount, polls open PRs into main every ~30 s. It uses ETag
  conditional requests on the reviewer App's rate pool and needs no public
  webhook endpoint. For a new head SHA it creates the `factory review` check
  as in progress and sends the kagent `reviewer` agent an A2A task with
  message ID `factory-review/<pr>/<sha>`, idempotent like coding runs. The
  agent has its own binding, a read-only repository token and no push. It
  returns a structured verdict. The service then completes the check on that
  exact SHA and, on a pass with required CI green, enqueues. The agent never
  holds the reviewer App's credentials, and the broker mints the author's and
  the reviewer's tokens only for their own ServiceAccounts.
- **Connector.** It coordinates sessions, revision rounds and itsaplan
  status. It holds no merge authority: the reviewer's failing check becomes
  the revision round's feedback (`reviewerCheck {name, appId}`).
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
  the reviewer App `jomcgi-factory-review` (checks write, merge queues
  write, contents and pull requests read) owns review and enqueueing
  ("Merge authority"); Joe creates it.
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
