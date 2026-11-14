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
   them.** [V] Network policy must leave the edge, the kagent UI and the
   connector as the only bearer paths into the controller.
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

## Umbrella chart

`projects/factory/chart` is a published `helm_chart` (OCI
`ghcr.io/jomcgi/homelab/charts/factory`) with the connector and broker
images pinned at build time. `projects/factory/deploy` holds the homelab
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

**Ordering.** kagent CRDs are vendored in `crds/`; a PreSync/pre-upgrade
Job server-side-applies them so upgrades update CRDs. Waves: namespaces,
ServiceAccounts, 1Password items and the database (-2); secret-init and a
database-ready gate (-1); kagent, itsaplan api, broker (0); itsaplan web and
worker, connector (1); kagent CRs and the itsaplan bootstrap Job (2).

**State kept across upgrades.** itsaplan `APP_ENCRYPTION_KEY` and
`BETTER_AUTH_SECRET` come from 1Password and never change. Generated keys,
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

## Connector

Go, built with rules_go and apko, one replica with `Recreate` and a Postgres
advisory lock. Each takeover increments a writer epoch, and every external
side effect checks it first. kagent guarantees one non-quiescent task per
session and one execution per actor, which keeps one writer per worktree.

**Data**, in its own database: repository bindings, work items (issue,
binding, branch `factory/<KEY>`, kagent session and generation, state),
runs, delivery attempts (unique per run and attempt, A2A message ID, kagent
task, error class, evidence URI), pull requests (head SHA, merge-queue
state), review events (unique GitHub node ID, commit SHA, disposition),
cancellations and an outbox. Full transcripts and CI logs go to the
`factory-evidence` bucket.

**Dispatch.** Runs come from itsaplan assignment, mentions and delegation;
none of these is a Pro feature (status-triggered schedules are Pro and are
not used). Claim runs per agent key. A claimed run with an open attempt
is a transport redelivery: find the kagent task by message ID and attach,
never send again. A new send uses message ID
`itsaplan-run/<runId>/<decisionId>`, where the decision ID is the connector's
record of why it sends (initial, revision or engineering retry); for continuation sends, look the ID up
in the session history after an ambiguous response before any resend. The
repository and branch come only from configuration.

A work item keeps one kagent session whichever agent key delivers its run.
When a run for an existing work item arrives under a different agent, the
connector bumps the writer epoch, confirms the mapped session has no running
task (cancelling it first if needed), and continues in that session; a new
session is created only by the replacement path below.

**Heartbeat** every 60 s. On 404, read the run: canceled starts cancellation;
a higher attempt means this attempt is stale and stops writing. Progress is
posted as itsaplan run events with a persisted offset.

**Completion.** The connector opens or updates the one PR for the work
item from the pushed branch. A run result means the turn finished; the
issue becomes Done only from the observed merge. Engineering retries are
connector decisions bounded per work item; credential and quota failures
block with the reason instead of retrying.

**Review.** Poll owned PRs every 60 s (no public webhook). An event acts only
if its commit is the current head and its author is a trusted identity;
duplicates and stale heads are recorded. Requested changes start a
mention-run in the mapped session. A session whose runtime crashed is
restored with kagent `RevertSession` to its last checkpoint; an expired or
deleted one is replaced with context from the issue, PR and review threads,
and the replacement is announced.

**Merge.** When the required checks and the trusted reviewer succeed on the
current head, enqueue with GraphQL `enqueuePullRequest`. When the merge is
observed, mark the itsaplan issue Done with the issue, run, session, PR and
merge links; a merge-queue failure returns the work item to review.
Deployment health is a separate record.

**Cancellation** sends A2A `tasks/cancel` and stays pending until kagent
reports the task canceled or failed.

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
  [factory-broker]`. [V] kagent binds that origin's `authorization` header
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
  (401), `codex_login_required` (401: no grant, a dead refresh token, or the
  backend still refusing after one forced refresh), `codex_quota_exhausted`
  (429 with `resets_at` and `Retry-After`, from the `x-codex-*` headers or a
  `usage_limit_reached` body; a current exhausted window blocks before the
  backend is called), `codex_grant_unavailable` (503, transient). Other
  backend 429s pass through for Codex to retry. The connector treats the
  login and quota codes as credential and quota failures.
- **Refresh.** One grant, `codex-primary`, from the broker's own device
  login: `POST /admin/codex/login` returns the user code, `GET` reports
  state, `POST /admin/codex/refresh` forces a refresh. These require a
  projected ServiceAccount token (audience `factory-broker`) whose TokenReview
  username is in `tokenReview.adminSubjects`. The grant refreshes when under
  24 h from expiry or 7 days old, single-flight, written to the Secret
  before use, checked every 10 minutes without traffic. When a refresh fails
  but the stored access token is still valid, the relay keeps serving it and
  the grant state metric reports the failure. ActorJWT leaves no relay key
  to rotate.
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
  checks and statuses read, and `merge_queues: write` [I]. GitHub adds
  `metadata: read`; the broker rejects any token wider than requested.
- **Metrics** (`factory_broker_*`): grant state and expiry, refresh checks
  and the shared refresh counters, relay requests by outcome and status
  class, actor JWT rejections by reason, GitHub mints by kind, binding and
  outcome, and the shared quota gauges. Labels come only from fixed sets or
  configuration.

## Identities

| Who | How |
| --- | --- |
| Humans to itsaplan | Edge OIDC, then itsaplan's own OIDC client in Authentik; invite-only registration; a break-glass admin |
| Humans to kagent | Edge OIDC with the access token forwarded; kagent in trusted-proxy mode. Sign-in alone grants nothing: the edge admits only the Authentik operators group, and kagent session ownership limits each user to their own sessions |
| Connector to kagent | Projected ServiceAccount token, kept ServiceAccount |
| Connector to itsaplan | A coordinator external agent for comments and issue state; the coding agent's key claims runs |
| Connector and agents to GitHub | Broker-minted App tokens, one repository per binding |
| Independent review | The Semgrep reviewer as a required check bound to its App ID in the ruleset |
| Workloads | Substrate pod certificates and actor SPIFFE IDs; no SPIRE |

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
- **GitHub App** `jomcgi-factory` in jomcgi-org: Contents and Pull requests
  read/write, Merge queues read/write [I], Checks, Commit statuses and
  Metadata read; installed on
  `jomcgi-org/homelab`; 1Password item `k8s-homelab/factory-github-app`
  (`app-id`, `installation-id`, `private-key`).
- **Semgrep reviewer.** App ID, check name, triggers, and adding it to the
  ruleset.
- **1Password.** `k8s-homelab/factory-itsaplan` (`BETTER_AUTH_SECRET`,
  `APP_ENCRYPTION_KEY`) and `k8s-homelab/factory-itsaplan-admin`
  (`email`, `password`).
- **Hostnames.** `factory.jomcgi.dev` stays the private entry point and links
  to `factory-plan.jomcgi.dev` (itsaplan) and `factory-agents.jomcgi.dev`
  (kagent).

Everything except live Codex runs, agent pushes and required review can be
built and trialled before these arrive.
