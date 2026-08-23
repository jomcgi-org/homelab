# MCP Gateway: current architecture

How the MCP surface works today. This is the source of truth for current state
and the thing to link to. The ADRs in the map at the bottom are rationale: they
record what was decided, not what shipped.

## Topology

**Context Forge is the single front door.** Every MCP caller reaches it, in
cluster and out, and it decides which tools that caller sees. There is no
second entry point.

```
Claude.ai / Claude Code ──► Cloudflare Tunnel ──┐
                                                ├──► Context Forge ──► registered upstreams
in-cluster agents ──────────► ClusterIP ────────┘        (mcp ns)         ├── monolith /mcp
                                                                          └── GitHub
```

Context Forge is IBM [mcp-context-forge](https://github.com/IBM/mcp-context-forge),
deployed as an ArgoCD Application in the `mcp` namespace. It wraps the upstream
`mcp-stack` subchart; there is no custom container image in this repo.

**The monolith's `/mcp` is a registered upstream, not an entry point.** It is a
shared `FastMCP` instance (`projects/monolith/core/mcp_app.py`) that domain
modules populate with `@mcp.tool` (`cluster`, `agent`, `agent_sessions`,
`knowledge`, `sandbox`, `semgrep_scan`). `framework/core.py:507` mounts it at
`/mcp` for any domain declaring `mcp_enabled`. It is always mounted when
enabled, so an empty tool list is a visible symptom rather than a missing route.

**Why.** Separate MCP servers originally required per-service authentication
workarounds and were unreachable from remote in-cluster agents, so ADR agents/003
chose one federating gateway. ADR agents/020 later rejected that gateway after
the deployment fell to one backend and retained a manual catalogue-refresh cost;
ADR agents/059 superseded 020 after its authentication premise changed, while
the reasoning against a redundant second gateway still holds. The current
topology retains the earlier mechanism until that cutover, accepting a gateway
failure as a tool-wide outage and upstream maturity as an operational risk.

## Identity decides which tools you see

Entitlement is expressed as **tags on tools**, not as a server or a URL per
audience. `list_server_tools` filters per caller from `tools.visibility` and
Context Forge team membership, so one virtual server (`homelab-admin`) can carry
the full monolith catalogue and still show each caller only their subset.

Group matching runs against the authentik `groups` claim by **name**, so
renaming a group in authentik silently stops matching. The names are not ids.

Which group lands in which team is declared in Git: `teamMapping.teams` in
`context-forge-gateway/deploy/values.yaml`, reconciled every 15 minutes by
the `team-mapping` CronJob (`templates/team-mapping-cronjob.yaml`). The job
creates any declared team that is missing and writes
`sso_providers.team_mapping` (group name to team id) only when it differs, so
a new group in values grants on the next tick and a Context Forge database
rebuild self-heals. Membership itself is never written by the job: Context
Forge applies the mapping per request from the token's `groups` claim. The
`sso_providers` row itself (client id, issuer, `trusted_for_api_auth`) is
still created by hand with `scripts/provision-mcp-auth.sh`, and the job fails
loudly while it is absent rather than inventing one.

The ACL is **tool-granular**: it decides whether you may call
`search_knowledge`, never what `search_knowledge` returns. Per-caller scoping of
results is a separate concern, and is not live (see Token forwarding).

**Why.** A single unrestricted catalogue gave every caller the same tools and
could not attribute operations to one agent identity (ADR agents/005). Separate
servers per role were rejected because they duplicate deployments and
configuration, while route-only tiering was rejected because callers reaching
one host and port can still guess another path (ADR agents/034). ADR agents/005
is deprecated and agents/034 remains draft, but their reasoning for tool-level
entitlement still explains the current tag and team model; result-level
authorization remains outside that model.

## Routing: isolation comes from authorization, not paths

The gateway root `/mcp` serves the **full** tool catalogue. Per-tier hostnames
(`scopedRoutes`, `templates/httproute-scoped.yaml`) each reach exactly one
virtual server, and each route is an allowlist of two rules, so every other path
on that hostname 404s. **A tier hostname must never route `PathPrefix: "/"`.**

Those routes are deliberately **path-transparent** rather than rewriting `/mcp`
onto `/servers/<id>/mcp`. RFC 9728 derives the protected-resource metadata URL
from the resource path, and Context Forge builds that URL from the path it
receives. Under a rewrite the client asks for
`/.well-known/oauth-protected-resource/mcp` while the server publishes
`/.well-known/oauth-protected-resource/servers/<id>/mcp`, and OAuth discovery
fails before the client ever contacts the authorization server. Transparency is
a protocol requirement here, not a preference.

`friends.jomcgi.dev` reaches `friends-empty`, a virtual server that is
deliberately empty so `tools/list` returns `[]`. It has no Cloudflare Access
application in front of it, and it exists only to test whether a Claude
connector can complete an OAuth flow against authentik without spending Zero
Trust seats. **Do not add tools to it.** The hostname exposing nothing is the
control.

Note that **`MCP_REQUIRE_AUTH` is gateway-wide, not per virtual server**, so it
cannot be the thing that isolates one hostname from another. That is what makes
"the server is empty" the actual control here, and it is why the in-cluster
ClusterIP path is no longer unauthenticated either.

**Why.** Path-shaped tool tiers alone do not form an authorization boundary
because a caller with network reach can request another tier's path (ADR
agents/034). Rewriting the resource path was also rejected because OAuth
protected-resource discovery must describe the same path the client requested
(ADR agents/059). The deployed design therefore combines an allowlisted route,
an authorized virtual server, and path transparency, accepting that gateway-wide
authentication cannot distinguish one virtual server from another.

## Authentication

External identity is authentik. Two settings are required and **neither is
sufficient alone**:

- `SSO_API_TOKEN_AUTH_ENABLED` gates external-IdP bearer auth. The check runs
  before the token is decoded, so with it false every authentik token 401s as
  "Invalid token" with no `verify_credentials` log line to explain why.
- Each provider must also set `trusted_for_api_auth`, which the provisioning
  script does.

`SSO_ENABLED` is a different thing: it mounts the `/auth/sso` router and the
browser SSO flow, and gates the admin API that manages the `sso_providers` row.
It does not gate the auth path itself.

NetworkPolicy is disabled. Authorization is enforced at the application layer.

### Effective values, and why you cannot read them off one file

The auth flags are split across two mechanisms with different precedence, so
neither `chart/values.yaml` nor `deploy/values.yaml` tells you the answer alone.
Verified against the running pod:

| Flag | Effective | Source |
|---|---|---|
| `MCP_REQUIRE_AUTH` | **true** | `extraEnv`, overriding the `secret:` block's `false` |
| `SSO_ENABLED` | true | `extraEnv` |
| `SSO_API_TOKEN_AUTH_ENABLED` | true | `extraEnv` |
| `AUTH_REQUIRED` | true | `secret:` via `envFrom` |
| `MCP_CLIENT_AUTH_ENABLED` | true | `secret:` via `envFrom` |
| `TRUST_PROXY_AUTH` | true | `secret:` via `envFrom` |
| `TRUST_PROXY_AUTH_DANGEROUSLY` | false | upstream default, never set here |

`TRUST_PROXY_AUTH` and `PROXY_USER_HEADER` date from the Cloudflare Access era
and are not the current auth model. They are retained rather than removed, so do
not read them as describing how a caller is authenticated today.

Two things follow. **`MCP_REQUIRE_AUTH` reads as `false` in `chart/values.yaml`
and is `true` in production**, because `env` outranks `envFrom`. And the whole
`AUTH_REQUIRED` / `MCP_CLIENT_AUTH_ENABLED` / `TRUST_PROXY_AUTH` /
`PROXY_USER_HEADER` block still lives in `secret:`, which is the location this
chart's own configuration rule says never to use: editing it does not roll the
deployment, so a change there sits in Git looking applied while the process
keeps the old value.

To read the real state, read the pod, not the values files:

```bash
kubectl get pod -n mcp -l app.kubernetes.io/name=mcpgateway \
  -o jsonpath='{.items[0].spec.containers[0].env[*].name}'
```

**Why.** Static edge service tokens blocked browser OAuth clients, shared one
credential across sessions, and split enforcement across two authentication
systems (ADR agents/006). ADR agents/011 superseded 006 by moving OAuth to a
managed edge service to remove an auxiliary stateful proxy; that mechanism was
later deprecated when the route moved to direct identity-provider tokens. ADR
agents/059 now decides that the monolith will verify those tokens directly
because the current gateway performs a redundant verification on every call,
accepting a staged cutover and connector-handshake risk.

## Token forwarding to upstreams, and what is not live yet

The caller's authentik token is forwarded to upstream gateways so the monolith
can learn who is calling. This is deliberately a **token, not a header**: a
header asks the monolith to believe anything that can reach it in cluster, while
an RS256 token is verifiable against authentik's JWKS, so a compromised gateway
cannot forge one.

Forwarding works because the monolith gateway has no gateway-level auth
(`gateways.auth_type` empty), which selects the pass-through branch in
`passthrough_headers.py`. `DEFAULT_PASSTHROUGH_HEADERS` stays empty, so
`Authorization` is the only thing forwarded.

**The monolith validates the forwarded token as of #4955.** `projects/monolith/auth`
verifies RS256 against authentik's JWKS on the `/mcp` mount and hands handlers a
`Principal`. Absent bearer material yields an anonymous least-privilege
principal rather than a 401, because Context Forge's health-check refresh and
gateway federation call with no user context; material that is present and
invalid always raises.

The token names the monolith as a **second audience**, so what arrives is
validated rather than believed. `blueprints/mcp-auth.yaml` binds a scope mapping
returning `{"aud": [provider.client_id, "https://private.jomcgi.dev"]}`, and the
monolith checks membership in that list alongside a strict `iss`. This is weaker
than RFC 8693 token exchange, which would mint a separate token per audience so
a backend could not replay it elsewhere. Context Forge implements exchange but
authentik does not advertise the grant, so the replay path is closed on the
network instead, by the CiliumNetworkPolicy in the monolith chart.

**Per-caller result scoping still does not work** (#4569), and validation being
live is not what stands in the way. Two things do:

- Verification runs, but on the claude.ai path no token has been observed
  reaching it: calls arrive with no `Authorization` header, so the principal is
  anonymous and there is no identity to scope by. A first verification forces a
  JWKS fetch, so on a freshly rolled pod
  `hubble observe --from-pod monolith/<pod> --to-namespace authentik` returning
  nothing means nothing has been verified. Beyond the first fetch the cache
  holds for `AUTH_JWKS_CACHE_TTL_S`, so silence stops being evidence once the
  pod has been up a while.
- Identity had to reach the tool, not just the middleware. On SSE, and on
  stateful streamable HTTP, the server task is started by the session opener
  and every later message runs in its context, so `current_principal()` stayed
  pinned to the opener (#4569 records the mechanism). The mount is now
  stateless streamable HTTP (`stateless_http=True` in `framework/core.py`), a
  transport per request in that request's own task, so the principal a tool
  reads is the caller of that message. A stateful mount would reintroduce the
  pinning silently.

**Why.** Tool-level gateway ACLs cannot decide whether a returned task, session,
or repository object belongs to the caller, so identity must reach the domain
that owns that object (ADR agents/055, ADR agents/059). Trusted identity headers
were rejected because an in-cluster caller could forge them; a verifiable bearer
keeps the resource server responsible for validation. ADR agents/059 superseded
055 for GitHub mediation by moving the broker into the monolith, while 055's
reasoning for tool mediation and bounded credentials still holds.

## Deployment

**This service deploys from a git path at `targetRevision: HEAD`, not from an
OCI chart.** That is a deliberate exception to the pattern documented for new
services. ArgoCD reads `projects/mcp/context-forge-gateway/chart` directly and
deep-merges `deploy/values.yaml` on top.

The practical consequence: **merging deploys instantly**, and there is no chart
version bump in the loop. Nothing waits for a `chart-version-bot` write-back.

**Why.** ADR agents/003 chose a configuration-driven upstream gateway so a new
tool could be registered through values and reconciled by ArgoCD instead of
shipping another local proxy or MCP binary. Per-service proxies were rejected
because authentication workarounds and Bash permissions multiplied with every
backend. That choice accepts the upstream gateway as a single point of failure
and keeps its pinned release and configuration portability as the recovery path.

## Configuration discipline

**Anything set deliberately goes in `extraEnv`, never in `config` or `secret`.**
Two independent traps make the alternatives silently wrong:

1. `config` and `secret` render into a ConfigMap and a Secret consumed via
   `envFrom`. The `mcp-stack` pod template carries no annotations block, so
   there is no checksum to change, and editing either object does not roll the
   deployment. The process keeps the old value indefinitely while Git, the
   ConfigMap and `kubectl rollout status` all look correct.
2. The chart emits some keys into **both** objects, and the `envFrom` order
   `[secret, configMap, secret]` shadows them back to defaults.

Kubernetes `env` outranks every `envFrom` source, so `extraEnv` closes both.

`SSRF_ALLOWED_NETWORKS` is set to the k3s pod and service CIDRs. Upstream
enables SSRF protection with private networks refused, so registering any
in-cluster MCP server otherwise fails with a masked 422 whose real cause appears
only in the log as `loc=('body','url') type=value_error`.

**Why.** The gateway was adopted to make backend registration a configuration
change rather than a custom server build (ADR agents/003). Local proxies for
each service were rejected because they repeat authentication and error-handling
work, while unconstrained registered endpoints create an SSRF path into backend
APIs. The accepted consequence is strict, chart-specific configuration with an
explicit network allowlist, read-scoped backend credentials, and a pinned
upstream release (ADR agents/003).

## Tool catalogue refresh

Context Forge caches each gateway's tool catalogue in its own Postgres. The
health-check loop refreshes that cache only when `AUTO_REFRESH_SERVERS` is true,
and `GATEWAY_AUTO_REFRESH_INTERVAL=600` limits each gateway to one refresh every
600 seconds. A refresh requires a successful health tick, and the health-check
path supports streamable HTTP gateways only.

The monolith registration lives in Context Forge's Postgres, not in git, and is
updated by hand: transport `STREAMABLEHTTP`, URL
`http://<monolith svc>/mcp/`. The slashed form is load-bearing because
`POST /mcp` answers 307 to `/mcp/`; a client that does not replay the body on
redirect looks like a dead gateway. The old SSE registration never completed a
health tick, so built-in refresh never ran. The tool-refresh CronJob was retired
in #5035 once the streamable HTTP tick was observed advancing `last_seen`.

No bearer arrives at the monolith on this refresh path, so discovery runs as
anonymous, which the monolith permits. Retiring the CronJob also removes Job
failure semantics: built-in failures are warning only, and a tool rejected by
Context Forge's XSS validator can be dropped silently. Operational proof is
`last_refresh_at` advancing on the monolith `gateways` row with no Job, plus a
tool count check for an incomplete catalogue.

**Why.** Context Forge's cached catalogue made new monolith tools invisible until
a refresh, one of the standing costs used to justify its removal (ADR agents/020).
Keeping the gateway was rejected once it had one backend and no remaining
federation value; ADR agents/059 superseded 020 after the gateway became an active
token verifier, while the stale-catalogue reasoning still holds.

## State

Postgres via CloudNativePG (`templates/postgres-cnpg.yaml`). Context Forge holds
its own gateway registrations, team membership, tool catalogue cache and
`sso_providers` there. Of that, only `sso_providers.team_mapping` and the
teams it names are reconciled from Git (see Identity above); the rest is
restored by hand after a rebuild. Secrets sync from 1Password via
`OnePasswordItem`.

**Why.** The original gateway consolidated remote access, authentication
workarounds, and virtual tool registration in one in-cluster component (ADR
agents/003). Separate local servers were rejected because remote agents could
not reach them and every backend needed its own access workaround. Consolidation
accepts a stateful catalogue and a gateway-wide failure domain, risks ADR
agents/020 and ADR agents/059 retain as reasons for the planned direct-monolith
cutover.

## ADR map

Rationale only. None of these describes current state.

| Decision | ADR | Status |
|---|---|---|
| Adopt Context Forge as the MCP gateway | `agents/003` | Superseded by 020 |
| Deprecate Context Forge, serve MCP from the monolith | `agents/020` | Accepted, **execution deferred** (issues #3831, #3832, #3833). The monolith `/mcp` exists; Context Forge is retained and `mcp.jomcgi.dev` is untouched, so this component is on a path to removal that has not been walked |
| Tool-mediated GitHub access for agent principals | `agents/055` | Draft. Makes this gateway the mediation point for agent GitHub access, which **cuts against 020**. The tension is stated in 055, not resolved |
| OIDC auth for the gateway | `agents/006` | Superseded by 011 |
| Cloudflare Managed OAuth | `agents/011` | Deprecated |
| Role-based MCP access | `agents/005` | Deprecated |
| Per-tier guest MCP ACL | `agents/034` | Draft |
| Agent MCP v1 follow-ons | `agents/042` | Accepted |
