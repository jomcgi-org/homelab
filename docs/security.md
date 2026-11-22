# Security Model

Current state of the controls that hold the homelab together, and where
each one is enforced. Decision rationale lives in the **Why** paragraph
under each section. Built controls, committed deployment gates and dated live
observations are distinct; repository configuration alone proves no rollout.
Findings are recorded privately. The [threat model](THREAT-MODEL.md) lists assets and controls.

Read this first for any security-sensitive change. For anything that
serves data on the public apex, read
[`docs/runbooks/public-tier-checklist.md`](runbooks/public-tier-checklist.md)
next. Platform mechanics (ingress library, delivery, storage) are in
`projects/platform/ARCHITECTURE.md`.

## Where things run

The GKE hub deploys the applications in `projects/gke-apps/kustomization.yaml`. EmberVM and the hub factory conductor have retired (#8063). The home factory runs its own connector, reviewer, sandbox and credential broker. See the [factory trust boundaries](THREAT-MODEL.md#trust-boundaries).

## Trust boundaries

Six lanes, each a different audience and a different gate. The hostnames
are the live `HTTPRoute` set on the shared Gateway (`kubectl get httproutes -A`).

| Lane | Host | Who | Gate |
|------|------|-----|------|
| Public | `jomcgi.dev` | anyone | none at the edge; served by a separate binary and Postgres roles (below), rate-limited per route |
| Friends | `friends.jomcgi.dev` | authentik `family` on `/moving`, `homelab-admin` on `/preview/`, any active human account with an email on `/grimoire` | Envoy `SecurityPolicy` doing authentik OIDC. `/moving` and `/preview/` add a `groups` claim check with `defaultAction: Deny`. `/grimoire` validates only the ID token's issuer and audience; authentik's `grimoire-human-access` policy decides admission, and `/grimoire/join` is anonymous and rate-limited. No Cloudflare Access application |
| Private | `private.jomcgi.dev` | Joe, plus service tokens | Cloudflare Access at the edge, then a `SecurityPolicy` validating the Access JWT on the monolith and Kargo routes |
| Machine | `mcp.jomcgi.dev` | MCP clients | no `SecurityPolicy`; Context Forge validates the authentik bearer itself |
| Identity provider | `auth.jomcgi.dev` | anyone, for the login flow | authentik's own session and MFA; the admin console is behind Cloudflare Access, configured in the dashboard |
| Tailnet | the Tailscale operator | tailnet members | the Kubernetes API through the operator's API-server proxy, and the monolith API as `monolith` on the tailnet |

(see: `projects/platform/cf-ingress-library/`, `projects/monolith/chart/templates/httproute-*.yaml`, `projects/mcp/context-forge-gateway/chart/templates/httproute-*.yaml`, `projects/platform-gke/tailscale/`)

**Why.** Per-service hostnames were rejected because each one added DNS and
optional SSO by hand; audience tiers make private exposure the default and
public exposure explicit (ADR networking/002). The friends lane exists
because the Cloudflare Access policy on the private host carries no group
concept and is shared with the operator tools, so a narrower grant there was
one binding mistake away from Kargo; a hostname that carries nothing else is
the boundary (ADR security/006). Moving the operator tools behind authentik
was deferred, not rejected, because an authentik outage must not lock the
operator out of the tools used to fix authentik. The tailnet lane adds a
second credential that reaches the API server with no Google login; the
tailnet ACL is the other half of that control, and the binding grants only
what the same person already holds.

## Identity

**authentik is the standing identity provider.** Blueprints in
`projects/platform/authentik/blueprints/` define one OAuth2 provider and
one application per lane (`dev`, `kargo`, `preview`, `moving`, `grimoire`, `mcp-friends`,
`mcp-agents`), each bound to a group (`homelab-admin`, `family` or `kg-agents`) or, for
Grimoire, to the `grimoire-human-access` policy. The `mcp-friends`
application is the exception: no blueprint creates it, and `mcp-auth.yaml`
only binds groups to it and patches its provider's scope mappings. Renaming a group in authentik
silently breaks every lane bound to it: the `groups` claim is matched by
name. authentik runs on the hub with its own CNPG cluster and publishes
`auth.jomcgi.dev` with no `SecurityPolicy`, because the login and setup
flows share one API prefix and cannot be gated by path.

**The monolith verifies, it does not issue.** `projects/monolith/auth/`
validates configured authentik standing issuers and hands opted-in handlers a
`Principal`. The complete current decision, exact claim checks, surface-specific
missing and invalid token behavior, owner authorization, and result-access gaps
are recorded in `projects/monolith/ARCHITECTURE.md` section 7 (#4940, #4941).
In particular, anonymous catalogue discovery on the shared MCP mount does not
authorize an anonymous tool call, and the agent MCP mount rejects anonymous
requests. `Authority.DELEGATED` and actor/scope fields are representable but no
delegation is minted or verified.

**Cloudflare Access guards the private tier.** The Access application and
its wildcard policy on `private.jomcgi.dev/*` live in the Cloudflare
dashboard; the repo carries only the verification half,
`projects/platform/cf-ingress-library/templates/_security-policy.tpl`, which
renders an Envoy `SecurityPolicy` that validates `Cf-Access-Jwt-Assertion`
and projects the `email` claim to `X-Auth-Email`. Live consumers on the hub:
`monolith-private-cf-access` and `kargo-private-cf-access`
(`kubectl get securitypolicies -A`). ArgoCD has no UI route on the hub
(`cfIngress.enabled: false` in `projects/platform/argocd/values-gke.yaml`);
the UI is reached through the Kubernetes API over the tailnet, and only the
push webhook route below is exposed here.

Documented holes in that gate, each deliberate:

- `/webhooks/github/argocd` is the ArgoCD push-sync webhook route, enabled on
  the hub by `cfIngress.githubWebhook.enabled: true` in
  `projects/platform/argocd/values-gke.yaml`. It is a separate route with no
  `SecurityPolicy`, reachable through an IP-allowlist bypass, so GitHub
  push events reach `argocd-server` instead of falling through to the
  monolith's private route. ArgoCD treats it as an unsigned refresh trigger.
- `/img/` on `private.jomcgi.dev` is the public tier's imgproxy route and
  carries no `SecurityPolicy`; the edge Access policy is the only gate.
- `friends.jomcgi.dev` has no Access application. The authentik
  `SecurityPolicy` is the only control, and `/.well-known/` and `/servers/`
  on that host are unauthenticated by design so OAuth discovery works
  (`projects/mcp/context-forge-gateway/chart/templates/httproute-scoped.yaml`).

**Identity headers are stripped at the listener.** Both lanes project a
verified claim into `X-Auth-Email`, and Envoy's JWT filter appends rather
than replaces, so the gateway-wide `ClientTrafficPolicy` removes any inbound
`X-Auth-Email` before either auth filter runs
(`projects/platform/cloudflare-gateway/templates/client-traffic-policy.yaml`).
Handlers on the friends lane read the header for "whose view" only;
authorization is the `groups` claim inside the verified token.

**MCP identity.** Context Forge forwards the caller’s authentik bearer to the monolith. The shared MCP surface checks the operator group in `core/mcp_policy.py`. Knowledge search applies caller-derived scope filters in `knowledge/retrieval_policy.py`. Individual tools govern access to their results.

**Discord features are allow-list only** (`projects/monolith/chat/acl.py`,
the `discord_feature_grant` table).

## Network

**Nothing is exposed directly.** The only ingress is two `cloudflared`
replicas (`projects/platform/cloudflare-gateway/values-gke.yaml`) with an
empty route list: every hostname falls through `catchAll` to the Envoy
Gateway `cloudflare-ingress`, and per-chart `HTTPRoute`s decide what a host
and path reach. There is no `Service` of type `LoadBalancer` outside
`kube-system` and no `Ingress` object; the route table is the allowlist.
Tunnel protocol is pinned to `http2`. The Tailscale operator is the second
way in: it publishes the Kubernetes API to the tailnet and impersonates the
tailnet identity as a Kubernetes user bound to `cluster-admin` for the
admins listed in `projects/platform-gke/tailscale/values.yaml`, exposes the
monolith API as a tailnet host, and declares one egress Service for the home
inference host on port 8090. The values file records the intended destination.
End-to-end reachability is unverified.

**GKE Dataplane V2 is the CNI, and there is no enforced private-monolith
egress policy on the hub.**
The managed dataplane exposes no `CiliumNetworkPolicy` or
`CiliumClusterwideNetworkPolicy` CRD (`kubectl api-resources | grep cilium`)
and reports `Encryption: Disabled`, so nothing on the wire is encrypted by
the cluster and only Kubernetes `NetworkPolicy` can be expressed. The inert
Cilium templates and policy-only values were removed in #5816 from monolith,
monolith-public, monolith-agents and EmberVM. Removal changes no GKE rendered
objects and does not provide replacement enforcement.

The chart carries a native Kubernetes `NetworkPolicy` egress arm for the
private app endpoint, but the GKE overlay leaves it disabled. Kubernetes
NetworkPolicy has no additive audit mode, so the policy remains staged until
GKE network-policy logs cover a representative window, including periodic
leader jobs and the secret-backed ICAL feed. Its API and DNS address lists are
also empty until an enabling change validates them against the live hub.

When enabled, required internal flows use namespace plus pod selectors, exact
ports, and a paired resolver address. Standard NetworkPolicy cannot match
FQDNs, so its public TCP 443 grant excludes private, loopback, link-local,
shared-address, and multicast ranges but cannot deny an arbitrary public HTTPS
host. That is an accepted GKE residual, not an exact replacement for Cilium's
FQDN rules. Metadata-server access would remain denied.

Native-policy enforcement has no open tracker: #5276 and #5277 closed not
planned. #3897 closed completed for staged default-off private policy support.
The staged native egress policy intentionally scopes default-deny to
the private app endpoint. Searxng, CNPG, Atlas migration jobs, and
the separate `monolith-workflows` batch namespace retain their existing egress
behavior because they are distinct workload and credential boundaries. They
need their own destination-specific policies instead of inheriting a union of
the app's privileges.

**One agent egresses to a third party.** The Polylane agent
(`projects/platform/polylane/`, hub only) holds a cluster-wide read: `get` and
`list`, no `watch` and no wildcards, over 24 resource types including
`pods/log`, `configmaps`, `serviceaccounts` and all four RBAC kinds. No
credential-bearing type appears in any rule, and its single write is
`get`/`update`/`patch` pinned by `resourceNames` to the one Secret it persists
its registration into, so it cannot mint a Secret. It reaches the vendor over
its own outbound `cloudflared`, so there is no inbound port and no exported
kubeconfig; revocation is deleting the Application or the API key. Pod log
content leaves the cluster on this path. Its `NetworkPolicy` is on and is
ingress-only by upstream's own rule: egress is `- {}`, which is what the tunnel
and the kube API need.

**The monolith-agents MCP tier has a separate repository-scoped observation role.** The
`monolith-agents` chart defines its own Roles for core workload status, events,
bounded logs, workload controllers, and pod metrics in an enumerated namespace
list. Argo CD Application reads are a Role in `argocd`; Kargo Freight reads are
Roles in the four hub Kargo Project namespaces. One independent ClusterRole has
only nodes, namespaces, and node metrics. All rules are get/list except the
get-only `pods/log`; there is no watch, mutation, Secret, exec, attach,
port-forward, proxy, wildcard group, wildcard resource, or reuse of the private
monolith ClusterRole.

The pruned `agent_kubernetes` package is the matching application boundary. It
constructs requests only from the same resource and namespace table, bounds
pages, log time and size, and API timeouts, and returns explicit freshness and
coverage. Every call requires the standing, undelegated `kg-agent-sa` workload
principal in `kg-agents`, even though the tier's four knowledge tools retain
their existing non-anonymous gate. The package cannot import `cluster`, and the
private import guard continues to enforce that exclusion.

This is source and rendered-manifest state, not deployment evidence. The hub
uses a separately published and pinned chart. No permission is live merely
because this repository contains it.

**Host-level exceptions, all named** (`kubectl get pods -A -o json`,
filtered for `privileged`, `hostNetwork`, `hostPID`, and uid 0):

| Setting | Where | Why |
|---------|-------|-----|
| `hostNetwork`, `hostPID`, root init containers | SPIRE agent DaemonSet, `projects/platform/spire/` | node attestation and the Workload API socket; upstream hardened chart with `securityContexts: true` |
| `privileged: true` | SPIFFE CSI driver, same chart | mounts the Workload API into pods |
| `privileged: true` | Tailscale proxy StatefulSets in the `tailscale` namespace | operator-managed; `NET_ADMIN` sysctls for the tailnet device |
| `runAsUser: 0` | `projects/inference/deploy/values-gke.yaml`, embeddings pod | the llama.cpp CUDA image runs as root |
| `NET_ADMIN` init container | `projects/platform/cloudflare-gateway/templates/tunnel-deployment.yaml` | inert; gated on `tunnel.envoy.enabled: false` |

**Why.** The former tunnel operator reimplemented routing while production
ran a static tunnel configuration, so Envoy Gateway took over in-cluster
routing and cloudflared narrowed to edge lifecycle (ADR networking/001).
Cilium was chosen on the home cluster because the sidecar mesh blocked
ordinary network policy and added a hop to every pod (ADR platform/012); the
move to the GKE hub kept a Cilium-based dataplane but traded its policy CRDs
and WireGuard for a managed one, an accepted cost recorded nowhere as a
decision.
## Secrets

Every external credential enters the cluster as an `OnePasswordItem` CR
synced by the 1Password Connect operator. The
operator is one of two components installed by hand: Helm release `connect`
in the `default` namespace, with no chart or values in this repo, and the
ArgoCD bootstrap release, which ArgoCD then adopts through the `argocd`
Application (`helm list -A`). Never hardcode a secret; values files reference
1Password item paths or a `secretRef`.

The one class of secret that cannot be declarative is the
`kubernetes.io/basic-auth` secret CNPG needs for a managed role, because the
operator emits only `Opaque` secrets. Each is created once from the same
1Password item the consumer reads, and each has a note in
`projects/monolith/deploy/` (`public-reader-secret.md`,
`public-writer-secret.md`, `spire-db-secret.md`, `embervm-oplog-secret.md`,
`agents-writer-secret.md`, `cnpg-gcs-backup-secret.md`).

CNPG backups use a GCS service-account key held in 1Password.

No automated check on secret hygiene runs in CI beyond Semgrep Managed
Scans' registry rules: the repo's own rules for a hardcoded secret or an
empty item path were removed (#4777), so review has to catch them.

(see: `projects/*/chart/templates/onepassworditem*.yaml`, `projects/monolith/deploy/*.md`)

**Why.** One external secret store with a CR per item keeps every
credential out of git and every consumer declarative; the hand-created
basic-auth secrets are the operator's limitation, not a choice, and each
one is documented at the point where it would otherwise be rediscovered by
a 503.

## Sandboxing

The former hub sandbox used EmberVM guests. Its Firecracker jailer and guest egress sidecar retired with that system. Factory workers now use the sandbox controls in the [factory threat model](THREAT-MODEL.md#trust-boundaries).

**Container security context is a convention, not a control.** The apko
images build as uid 65532 (`bazel/tools/oci/go_image.bzl`) and most charts
set the full hardened context (`readOnlyRootFilesystem`, `runAsNonRoot`,
`allowPrivilegeEscalation: false`, `drop: [ALL]`, `seccompProfile:
RuntimeDefault`). The private monolith applies that context to its backend,
progress-ingest and frontend containers. Each gets
a separate writable `/tmp` `emptyDir`, owned through pod `fsGroup: 65532`;
the remainder of each root filesystem is read-only. No admission policy
enforces any of it. Kyverno runs two `ClusterPolicy` objects, both `Audit`
(`kubectl get clusterpolicies`):
`require-resource-requests`, scoped to the `monolith` and `monolith-public`
namespaces, and `clone-monolith-workflows-secrets`, which copies Secrets
into the job namespace. The OTel injection policy is disabled. Nothing is
rejected at admission, and no check flags a new exception: the repo's
`no-privileged` and `no-host-network` Semgrep rules were removed (#4777).

## Public tier

`jomcgi.dev` is served by `monolith-public`, a separate chart and a separate
Python entrypoint (`projects/monolith/app/main_public.py`) whose import
closure is pruned in `projects/monolith/BUILD` and asserted by
`app/main_public_imports_test.py`. The rules that keep it fail-closed:

- **Reads go through `public_reader` on the replica.** `monolith-pg` runs
  two instances on the hub; the public service reads `monolith-pg-ro` as a
  `NOLOGIN` role created by CNPG `managed.roles`, and every public table
  needs an explicit grant (`projects/monolith/chart/migrations/*_public_reader_grant.sql`).
  A PreToolUse hook (`bazel/tools/hooks/check-public-reader-grant.sh`)
  blocks a new `CREATE TABLE` in a public schema without one; it fires only
  for edits made through Claude Code.
- **Constrained chat writes.** Grimoire chat writes as `public_writer` on the
  primary, scoped to its `grimoire_chat` tables; the retired notes-chat
  `chat_public` grants and tables remain (`chart/migrations/20260706200000_grimoire_chat.sql`
  and `chat_public_grants_test.py` under `projects/monolith/`).
  Sessions are Turnstile-bound with per-session token budgets and a global
  concurrency slot of one (`projects/monolith/grimoire_chat/limits.py`). On
  the hub, `grimoire_chat/inference.py` reads `GRIMOIRE_CHAT_INFERENCE_URL`
  with `CHAT_PUBLIC_INFERENCE_URL` as its fallback. The public hub values
  still set that fallback to Meta's external API. The public composition
  mounts Grimoire chat, reached through `/app/grimoire/chat` frontend proxies;
  the old notes-chat router and inference client are removed. Anonymous
  Grimoire prompts therefore use the configured off-cluster path, whose
  availability is not verified here.
- **Legacy notes-chat purge is built.** #3899 closed completed on 2026-10-01.
  `chat_public/retention.py` and `app/jobs_main.py` implement retention and
  selector-based takedown with audit counts, hashed selectors and dry-run
  rollback. The private chart schedules `chat-public-retention` at `15 4 * * *`
  with `suspend: false`; `chat-public-takedown` is manual-only and suspended.
  CronWorkflows render when the jobs image is pinned. Defaults remove
  snapshots older than 365 days and sessions idle over 30 days with no
  remaining snapshot, including their messages. These jobs cover
  `chat_public`, not `grimoire_chat`; no successful live purge is claimed here.
- **No `/api` on the public origin.** The public `HTTPRoute`s
  (`projects/monolith-public/chart/templates/httproute-public.yaml`) carry
  no `/api` rule, asserted by `public_httproute_chat_guard_test`; pages
  fetch through same-origin `+server.js` proxies. The paths that reach a
  backend from the internet are `/functions/` (the FaaS router, backend
  port), `/img/` (imgproxy, backed by R2), and `/health/otel-collector`. The functions route remains configured, but its EmberVM execution service has retired from the hub. Each route carries an Envoy
  `BackendTrafficPolicy` rate limit.
- **Row filtering is the query's job.** A grant admits the table; the query
  must filter (`is_global = true` or the public view).
- **Published docs pass a content gate.** `projects/monolith/knowledge/tools/public_content.py`
  fails the docs and posts generators on in-cluster hostnames, private
  addresses, node and brick names, bucket URIs, 1Password references and
  secret-shaped assignments. It is a fixed marker list: an identifier
  matching no marker publishes verbatim. #5275 closed completed for that
  content gate; this residual limitation has no open tracker.

The inert destination-scoped Cilium policy and its policy-only guard were
removed in #5816. Public native-policy enforcement has no open tracker
(#5276 closed not planned; Network above); the unrelated OTEL endpoint assertion survives in the public render
test. Kargo promotes `monolith-public` on the hub,
so the live `targetRevision` on the Application, not the git pin, says what
is deployed.

(see: `projects/monolith-public/`, `projects/monolith/app/main_public.py`, `projects/monolith/chat_public/`, `docs/runbooks/public-tier-checklist.md`)

**Why.** The anonymous SSR surface once shared a pod, backend, database role
and secret set with the private application, so a public-process compromise
crossed every boundary at once (ADR security/004). A feature-flagged single
image was rejected because the private code still ships and the isolation
becomes a runtime setting that can be wrong; the replica alone was rejected
as a confidentiality boundary because a standby replicates every row.
Separate compositions plus an engine-enforced role make private exposure
the default. Public chat added Turnstile sessions, server-side limits and
DB-confined retrieval because an anonymous generative endpoint is
adversarial by default and must never degrade the private surface (ADR
security/005).

## Findings and threat model

[`docs/THREAT-MODEL.md`](THREAT-MODEL.md) holds the structure of the threat
model for both clusters: assets, attacker positions, trust boundaries and
the controls on each boundary. It lists no open findings. A finding is
recorded in a private security project, and its fix is a HOME ticket in
neutral words. Depth lives in each project's STPA security lens
(`projects/<domain>/stpa/security.json`, rendered into `STPA.md`), where
every security row carries a `status` (`enforced-prod`, `enforced-dev`,
`shipped-off`, `designed`, `none`) and an `issue`. The procedure for adding,
closing and reviewing a finding is
[`docs/runbooks/threat-model-maintenance.md`](runbooks/threat-model-maintenance.md).

(see: `docs/THREAT-MODEL.md`, `docs/runbooks/threat-model-maintenance.md`, `projects/*/stpa/security.json`)

**Why.** Joe decided on 2026-11-17 that findings do not reach the public
repository
([decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-230#comment-2061)).
The earlier ranked index accepted the public cost because public GitHub
issues were the source of truth for each finding. Those issues moved to
itsaplan on 2026-11-15, so that reason ended. The first per-domain threat
model went stale within two days of merging (ADR security/007), and the STPA
security lens replaced per-domain prose models (#5294): it keeps each
project's analysis as extracted rows.

## Static checks and review gates

What a change is checked against, and by what. A row marked review-only is
a convention this document asks for and nothing enforces.

| Check | Mechanism | State |
|-------|-----------|-------|
| Semgrep registry rules | Semgrep Managed Scans, configured on the Semgrep side | live; not in this repo |
| Chart admissibility (kubeconform) | `//bazel/helm:chart_admissibility_test`, `chart-admissibility-test.sh` and `admissibility.bzl` | landed; validates the explicit `MANIFEST_SETS` in `bazel/helm/BUILD`: application/dev and platform default/hub renders plus Loom manifests, against pinned Kubernetes and CRD schemas; completeness checks retain explicit skips |
| Pod hardening and privilege | Kyverno | two policies, both `Audit`, neither about security context |
| Resource requests and a memory limit | Kyverno `require-resource-requests` | `Audit`, `monolith` and `monolith-public` only |
| Public binary import closure | `main_public_imports_test` | live in CI |
| Public route shape | `public_httproute_chat_guard_test` | live in CI |
| Public chat grants | `chat_public_grants_test` | live in CI |
| Public library image digests | `public_library_image_digest_guard_test` | live in CI |
| Published doc content | `public_content.py` in the Format stage | live in CI, marker list only |
| Retired ADR path guard | `bazel/tools/format/doc_links/check_doc_links.py` | live in CI |
| STPA security rows carry `status` and `issue` | the validator in `docs/agents/stpa.md` BLOCK A | at authoring time |
| `public_reader` grant, migration size, em-dashes | PreToolUse hooks in `.claude/settings.json`, `bazel/tools/hooks/` | Claude-side only; a hand edit or any other agent bypasses them |
| Chart `version:` and `targetRevision:` moved on a branch | `bazel/tools/ci/chart_version_guard.py` in PR checks | every author; lowering a pin (the revert lever) is allowed |
| `kubectl` read-only (Claude may write to the factory home cluster with a `KUBECONFIG=~/.kube/factory.yaml` prefix while it is bootstrapped) | `bazel/tools/hooks/block-kubectl-mutate.sh` for Claude's shell; `source_ratchet.py` for writes committed into scripts | no agent-identity RBAC yet, so another agent's shell is unguarded |
| Hand-pinned digests on repo-built images, in-cluster URLs in source, sync Session I/O in `async def` | `bazel/tools/ci/source_ratchet.py` in PR checks | new instances only; existing ones grandfathered; `ratchet-allow` opt-outs visible in review |

**Adding or changing a service, what to verify by hand**, because the
column above says nothing will do it for you:

- Non-root (uid 65532), `readOnlyRootFilesystem`, `allowPrivilegeEscalation:
  false`, `drop: [ALL]`, `seccompProfile: RuntimeDefault` in the chart. Any
  exception goes in the host-level table above with its reason.
- CPU and memory requests and a memory limit.
- What the pod reaches and what reaches it. On the hub only a Kubernetes
  `NetworkPolicy` is enforceable.
- Secrets only as `OnePasswordItem`; a CNPG role password is the one
  hand step and gets a note in `deploy/`.
- Ingress as an `HTTPRoute` on the shared Gateway with an `ingress-tier`
  label and a `SecurityPolicy` for anything not public; never a
  `LoadBalancer` or an `Ingress`.
- A private monolith endpoint that reads cluster resources needs matching
  permissions in `projects/monolith/chart/templates/rbac.yaml`. An agents-tier
  observation instead belongs in its independent namespaced Roles and narrow
  cluster exception, never the private ClusterRole.
- Anything served on the public apex: the public-tier checklist.
- Identity comes from a verified claim or token, never from a header a
  caller can set; hand-pinned image digests go stale after the next
  rebuild.

(see: `bazel/tools/hooks/`, `projects/monolith/BUILD`, `.claude/settings.json`)

**Why.** Semgrep was moved into Bazel so a rule would run as a cached test
with hermetic, digest-pinned inputs (ADR security/001). Its fail-open wrapper
let the engine stop scanning without a red, so for months the repo's 93
custom rules enforced nothing, and the semgrep guest that also carried them
went unused. Both were removed (#4777) in favour of Semgrep Managed Scans,
which is why the list above states enforcement rather than intent.

## Where each control is enforced

| Control | Enforced in |
|---------|-------------|
| Ingress allowlist | `projects/*/chart/templates/httproute*.yaml`, rendered onto `cloudflare-ingress` |
| Cloudflare Access JWT | `projects/platform/cf-ingress-library/templates/_security-policy.tpl` |
| authentik OIDC lanes | `projects/platform/authentik/blueprints/`, `projects/monolith/chart/templates/httproute-friends.yaml`, `projects/mcp/context-forge-gateway/chart/templates/httproute-preview.yaml` |
| Identity header strip | `projects/platform/cloudflare-gateway/templates/client-traffic-policy.yaml` |
| Bearer verification | `projects/monolith/auth/`, `projects/monolith-agents/` (`auth:` values) |
| MCP tool visibility | `projects/mcp/context-forge-gateway/deploy/values.yaml` (`tools.visibility`, teams) |
| Workload identity issuance | `projects/platform/spire/values.yaml` (`clusterSPIFFEIDs`) |
| Tailnet access | `projects/platform-gke/tailscale/values.yaml` (`apiServerProxyAdmins`, `egressServices`), `projects/monolith/chart/templates/service-tailnet.yaml` |
| Pod network policy | none on the hub beyond the Context Forge redis rule; `projects/monolith/chart/templates/networkpolicy-egress.yaml` is gated off in `values-gke.yaml` |
| Secrets | `OnePasswordItem` CRs per chart; operator is the `connect` Helm release outside git |
| Public data isolation | `public_reader` and `public_writer` grants, `main_public_imports_test`, `public_httproute_chat_guard_test`, `chat_public_grants_test`, `check-public-reader-grant.sh` |
| Public rate limits | `BackendTrafficPolicy` objects in `projects/monolith-public/chart/templates/` |
| Published content | `projects/monolith/knowledge/tools/public_content.py` |
| Review-time static checks | Semgrep Managed Scans (registry rules) |
| Findings register | Private security project and HOME fix tickets |

## Direction

Decided and not yet built, each with the issue that tracks it. A row leaves
this table when the work ships or the issue closes without it.

| Direction | Decided in | Tracks | State |
| --- | --- | --- | --- |
| Public-tier egress scopes to its four documented destinations, enforced on the hub | Network | #5276 | not started |

## Decision history

The ADR files were removed on 2026-09-06 (#4667); `git log -- docs/decisions/`
has the full text.

Rationale only; these records describe decisions, not current state. This
document carries what shipped.

| ADR | Decision | Status today | Disposition |
|-----|----------|--------------|-------------|
| security/001 Hermetic Semgrep via Bazel | vendor `semgrep-core` as an OCI artifact and run rules as cached Bazel tests | Superseded: the Bazel layer never scanned and was removed; CI scanning is Semgrep Managed Scans (#4777) | deleted |
| security/002 Semgrep rule generation via RL | RL-finetuned model generates rules from CVEs | Deprecated; nothing live | deleted |
| security/003 gVisor RuntimeClass | `runsc` for agent sandbox pods | Superseded by Firecracker guests; #3894 closed not planned | deleted |
| security/004 Public read-only service isolation | separate public composition, `public_reader` on a replica, default-deny egress | Composition, role, replica, imports test and private rootfs hardening shipped; #3897 completed staged default-off policy support; #5277 (private enforcement) closed not planned; umbrella #3895 closed not planned | deleted |
| security/005 Public chat adversarial hardening | Turnstile sessions, reserved headroom, server-side limits, DB-confined retrieval | Notes-chat retention and takedown built (#3899 completed); its public chat endpoint was later retired. Grimoire chat retains the budgets and off-cluster inference; the legacy purge covers only `chat_public` | deleted |
| security/006 Friends authorization lane | `/moving` on `friends.jomcgi.dev` behind an authentik `family` group | Accepted, shipped (#4968) | deleted |
| security/007 Aggregate threat model index | one ranked index over labelled issues, re-ranked by hand | Accepted; decisions 1, 2 and 4 live, decision 3 superseded by STPA lenses (#5294) | deleted |
