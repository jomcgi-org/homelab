# MCP Gateway: current architecture

How the MCP surface works today. This is the source of truth for current state
and the thing to link to. The ADRs in the map at the bottom are rationale: they
record what was decided, not what shipped.

Reconciled against the running hub on 2026-09-05. Where a claim below names a
live object, that is the date it was read.

## Topology

**There are two MCP entry points, split by caller, and they share no path.**
Context Forge is the front door for people and their tools: Claude.ai
connectors, Claude Code and the claude.ai routines all reach `mcp.jomcgi.dev`,
and it decides which tools that caller sees. Ember guests never reach it: they
talk to the monolith-agents tier, a second and smaller MCP surface with its own
port, binary and identity. Nothing else in the cluster calls Context Forge's
MCP endpoint. Its one in-cluster caller is the team-mapping CronJob, which uses
the REST API with an admin JWT it mints itself.

```
Claude.ai / Claude Code ──► Cloudflare ──► Envoy ──► Context Forge ──► monolith /mcp (port 8000)
                                                     (mcp ns, hub)      registrations: monolith (enabled)
                                                                        GitHub (disabled, no tools)

Ember guest ──► egress sidecar ──► monolith-agents /mcp (port 8092)
                (injects a broker-minted authentik bearer)
```

Context Forge is IBM [mcp-context-forge](https://github.com/IBM/mcp-context-forge),
deployed as an ArgoCD Application in the `mcp` namespace of the GKE hub, where
it has run since the home cluster was pruned on 2026-08-31. It wraps the
upstream `mcp-stack` subchart; there is no custom container image in this
repo. Its registered upstreams are the monolith and GitHub, but only the
monolith is enabled: the GitHub registration (the Copilot MCP endpoint, OAuth)
is disabled and carries no tools, so every tool a caller sees today comes from
the monolith.

The gateway emits no spans, so tracing reads this hop as absent, but its own
`duration_ms` logs measure it: on the hub a `search_knowledge` call logs about
a second and a half end to end at the gateway, of which the upstream call is
under half (2026-09-05 sample; ADR agents/059 recorded 330 to 780 ms per call
before the move).

**The monolith's `/mcp` is a registered upstream, not an entry point.** It is a
shared `FastMCP` instance (`projects/monolith/core/mcp_app.py`) that domain
modules populate with `@mcp.tool`. `framework/core.py` mounts it at `/mcp` for
any profile declaring `mcp_enabled`, as stateless streamable HTTP behind
`PrincipalMiddleware`. It is always mounted when enabled, so an empty tool list
is a visible symptom rather than a missing route. Which domains register is
listed in `projects/monolith/ARCHITECTURE.md`.

**The monolith-agents tier is the second entry point, for Ember guests only.**
It is a pruned monolith binary (`projects/monolith/app/agents_main.py`)
serving four knowledge tools (`search_knowledge`, `report_knowledge`,
`dispute_fact`, `report_distress`) on its own Service and port, with no
cluster RBAC and a database role scoped to the knowledge tables. It is
fail-closed: an anonymous principal is answered 401 before the MCP app runs.
Identity comes from the authentik `mcp-agents` provider by
`client_credentials`. The EmberVM token broker mints the bearer (grant
`agent-mcp`) and the brick-local egress sidecar injects it on the way out, so
the guest never holds the token and cannot reach the tier except through the
sidecar. The guest-side half (the egress allowlist entry, `plaintextUpstream`,
`injectAlwaysPaths`, the shim-written MCP client config and the boot argument
that carries the URL) is EmberVM's and is documented there.
(see: `projects/monolith-agents/chart/values.yaml`,
`projects/monolith/app/agents_main.py`, `projects/embervm/deploy/values.yaml`
under `egress.secrets`, `projects/embervm/deploy/values-gke.yaml` under
`kernelBootArgs`, `projects/embervm/ARCHITECTURE.md`)

**Why.** Separate MCP servers originally required per-service authentication
workarounds and were unreachable from remote in-cluster agents, so ADR agents/003
chose one federating gateway. ADR agents/020 later rejected that gateway after
the deployment fell to one backend and retained a manual catalogue-refresh cost;
ADR agents/059 superseded 020 after its authentication premise changed, while
the reasoning against a redundant second gateway still holds. The current
topology retains the earlier mechanism until that cutover, accepting a gateway
failure as a tool-wide outage and upstream maturity as an operational risk.
The agents tier is a second surface rather than a gate on Context Forge's root
because a new surface can be fail-closed from day one without risking the
endpoint in daily use, guests then never traverse the gateway or pay its
per-call cost, and its identity model (OIDC discovery, JWKS, `groups` claims,
authorization in the resource server) is portable where Context Forge teams
and `tools.visibility` rows are not (#5633, #5656).

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
still created by hand with `context-forge-gateway/scripts/provision-mcp-auth.sh`,
and the job fails loudly while it is absent rather than inventing one.

**What the team model enforces today: nothing.** Every one of the 57
registered monolith tools is owned by the platform administrator's personal
team, and the `homelab-admin` team owns none. The 53 tools a caller sees are
visible because their `visibility` is `public`, which skips the team check
entirely. The other four are `team`-visible and not associated with the
`homelab-admin` virtual server, so no caller sees them on `mcp.jomcgi.dev`
(on 2026-09-05: `grant_kg_burst`, `monolith_agent_run_decide`,
`monolith_codex_broker_refresh`, `submit_product_update`). Both columns are
hand edits in Context Forge's Postgres and exist in no repo file; Tool
catalogue refresh below lists the gates a new tool has to clear. #5633 moves
this filtering into the monolith, keyed on the principal's groups, and the
agents tier already works that way.

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

`mcp.jomcgi.dev` is the one place a rewrite is safe, because Envoy owns both
halves of discovery there: it serves the RFC 9728 document for `/mcp` itself
(`templates/httproute.yaml`), rewrites `/mcp` onto the `homelab-admin`
virtual server, and overwrites the `WWW-Authenticate` pointer so the client
only ever sees `/mcp`. The hostname is still behind a hostname-wide Cloudflare
Access application with a Bypass policy over `/mcp` and the discovery
document: an unauthenticated request to `/admin` is answered by Access (its
401 names a `cloudflare-access-protected-resource`), while `/mcp` reaches
Envoy and is answered by Context Forge with the RFC 9728 pointer. The control
plane keeps its edge layer and the data plane uses OAuth against authentik.

`friends.jomcgi.dev` reaches `friends-empty`, a virtual server that is
deliberately empty so `tools/list` returns `[]`. It has no Cloudflare Access
application in front of it, and it exists only to test whether a Claude
connector can complete an OAuth flow against authentik without spending Zero
Trust seats. **Do not add tools to it.** The hostname exposing nothing is the
control.

The same chart carries the browser-authenticated preview lane on
`friends.jomcgi.dev/preview/` (`templates/httproute-preview.yaml`). It is not
MCP: an Envoy `SecurityPolicy` runs the OIDC flow against authentik and denies
by default, and an unauthenticated request redirects to authentik rather than
serving anything.

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

Proxy-header identity is unreachable in this release: while
`MCP_CLIENT_AUTH_ENABLED` is true, upstream's `is_proxy_auth_trust_active`
returns false before any header is read, so the gateway never believes a
caller-supplied user header whatever else is set.

NetworkPolicy is disabled. Authorization is enforced at the application layer.

### Effective values, and why you cannot read them off one file

The auth flags are split across two mechanisms with different precedence, so
neither `chart/values.yaml` nor `deploy/values.yaml` tells you the answer alone.
Verified against the running pod on the hub:

| Flag | Effective | Source |
|---|---|---|
| `MCP_REQUIRE_AUTH` | **true** | `extraEnv`, overriding the `secret:` block's `false` |
| `SSO_ENABLED` | true | `extraEnv` |
| `SSO_API_TOKEN_AUTH_ENABLED` | true | `extraEnv` |
| `AUTH_REQUIRED` | true | `secret:` via `envFrom` |
| `MCP_CLIENT_AUTH_ENABLED` | true | `secret:` via `envFrom` |
| `TRUST_PROXY_AUTH_DANGEROUSLY` | false | upstream default, never set here |

Two things follow. **`MCP_REQUIRE_AUTH` reads as `false` in `chart/values.yaml`
and is `true` in production**, because `env` outranks `envFrom`. And the
`AUTH_REQUIRED` / `MCP_CLIENT_AUTH_ENABLED` pair still lives in `secret:`,
which is the location this chart's own configuration rule says never to use:
editing it does not roll the deployment, so a change there sits in Git looking
applied while the process keeps the old value. A key deleted from `secret:`
lingers in the process the same way until the next roll, so the pod's
environment can show a variable that Git no longer sets.

To read the real state, read the pod, not the values files:

```bash
kubectl get pod -n mcp -l app=context-forge-gateway-mcp-stack-mcpgateway \
  -o jsonpath='{range .items[0].spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}'
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

Forwarding is upstream behaviour, not this repo's. `get_passthrough_headers`
in Context Forge's `mcpgateway/utils/passthrough_headers.py` copies the
caller's `Authorization` onto the upstream call whenever the gateway row has
`auth_type` `none`, which the monolith registration does. That branch runs
before the `ENABLE_HEADER_PASSTHROUGH` check and before the header allowlist,
so the flag set in `deploy/values.yaml` is not what forwards the token: it
only admits the allowlisted extra headers, and the token would be forwarded
with it off. When a caller also sends `X-Upstream-Authorization`, that header
wins and is renamed to `Authorization` (#5002).

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
authentik does not advertise the grant. The replay path is **not** closed on
the network on the hub: the monolith chart's `tokenReplayDeny`
CiliumNetworkPolicy is off in `projects/monolith/deploy/values-gke.yaml`
because GKE Dataplane V2 ships no Cilium CRDs. The monolith has no code path
that calls the gateway, which is the only thing keeping that theoretical.

**The token now reaches the monolith.** On 2026-09-05 the hub monolith logged
principals resolved on `/mcp/` with `authorization_present=True`, kind human,
standing authority and the caller's authentik groups (the Claude.ai and Claude
Code path), interleaved with anonymous principals from Context Forge's own
health tick (`auth/middleware.py`, #5634). Identity also reaches the tool, not
just the middleware: the mount is stateless streamable HTTP
(`stateless_http=True` in `framework/core.py`), a transport per request in that
request's own task, so the principal a tool reads is the caller of that
message. On SSE, and on stateful streamable HTTP, the server task is started by
the session opener and every later message runs in its context, so a stateful
mount would pin `current_principal()` to the opener again (#4569 records the
mechanism).

**Per-caller result scoping is still not built** (#4569). `search_knowledge`
returns the same rows to an admin and to an anonymous caller. The one monolith
tool that reads the principal for authorization, `grant_kg_burst`, gates on
group membership rather than scoping results, and it is not reachable through
Context Forge today (see Identity). What stands in the way is now only the
scoping code itself.

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
services. The hub's Application
(`projects/gke-apps/context-forge-gateway/application.yaml`) reads
`projects/mcp/context-forge-gateway/chart` directly and layers
`deploy/values.yaml` and then `deploy/values-gke.yaml` on top, with the
subchart's schema validation skipped. `deploy/application.yaml` is the
home-cluster shape of the same Application and is referenced by no root.

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

`SSRF_ALLOWED_NETWORKS` is the seam that lets an in-cluster URL be registered
at all. Upstream enables SSRF protection with private networks refused,
resolves the hostname, and rejects any address not in this list, with the real
cause visible only in the log as `loc=('body','url') type=value_error`. The
check runs when a gateway row is created or updated, never on the health tick
or a tool call. The list still names the retired home cluster's pod and
service ranges, which cover nothing on the hub: the monolith row was
registered before the move and keeps ticking, but a new in-cluster
registration will be refused until the value is updated to the hub's ranges.

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

**Refresh puts rows in `tools`; it does not make them visible.** A new
monolith tool clears five gates before a caller sees it, and refresh covers
only the first two: the monolith serves it; the health tick syncs it; Context
Forge's XSS validator accepts the description (a bullet reading
`- javascript: ...` is read as a URI scheme and the tool is dropped while the
refresh still reports success); a `server_tool_association` row links it to
the `homelab-admin` virtual server, which refresh never writes; and
`visibility` is `public`, where refresh leaves a new tool at `team`. The last
two are hand edits in Postgres after every new tool, and four tools are
waiting on them as of 2026-09-05. #5636 keeps federation off this path
entirely by mounting upstream servers on the agents tier.

**Why.** Context Forge's cached catalogue made new monolith tools invisible until
a refresh, one of the standing costs used to justify its removal (ADR agents/020).
Keeping the gateway was rejected once it had one backend and no remaining
federation value; ADR agents/059 superseded 020 after the gateway became an active
token verifier, while the stale-catalogue reasoning still holds.

## State

Postgres via CloudNativePG (`templates/postgres-cnpg.yaml`). Context Forge holds
its own gateway registrations, virtual servers, tool catalogue cache, tool
association and visibility rows, team membership and `sso_providers` there. Of
that, only `sso_providers.team_mapping` and the teams it names are reconciled
from Git (see Identity above); the rest is restored by hand after a rebuild.
On the hub the cluster was bootstrapped by recovery from the home cluster's
GCS archive and archives under its own server name so the two never share a
prefix (`deploy/values-gke.yaml`). Secrets sync from 1Password via
`OnePasswordItem`.

**Why.** The original gateway consolidated remote access, authentication
workarounds, and virtual tool registration in one in-cluster component (ADR
agents/003). Separate local servers were rejected because remote agents could
not reach them and every backend needed its own access workaround. Consolidation
accepts a stateful catalogue and a gateway-wide failure domain, risks ADR
agents/020 and ADR agents/059 retain as reasons for the planned direct-monolith
cutover.

## ADR map

Rationale only. None of these describes current state. Every ADR here is
shared with the monolith rollup, which deletes them; none is deleted by the
mcp rollup.

| ADR | Decision | Status | Disposition |
|---|---|---|---|
| `agents/003` | Adopt Context Forge as the MCP gateway | Superseded by 020; the deployment is live | shared with monolith, left in place |
| `agents/005` | Role-based MCP access | Deprecated | shared with monolith, left in place |
| `agents/006` | OIDC auth for the gateway | Superseded by 011 | shared with monolith, left in place |
| `agents/011` | Cloudflare Managed OAuth | Deprecated; Access now fronts only the control plane | shared with monolith, left in place |
| `agents/020` | Deprecate Context Forge, serve MCP from the monolith | Superseded by 059; its execution issues #3832 and #3833 stay open, #3831 closed 2026-09-05 | shared with monolith, left in place |
| `agents/034` | Per-tier guest MCP ACL at `/private/mcp/{tier}/` | Draft; #3838 open. The agents tier (#5656) is the shape that shipped instead | shared with monolith and embervm, left in place |
| `agents/042` | Agent MCP v1 follow-ons | Accepted, partially shipped; #3844 | shared with monolith, left in place |
| `agents/055` | Tool-mediated GitHub access on Context Forge | Superseded by 059; mediation moves to the monolith's broker (#4946). The GitHub registration here is disabled with no tools | shared with monolith, left in place |
| `agents/059` | Authentik federates identity, the monolith serves MCP directly | Draft, not executed: Context Forge is still the front door, `mcp-friends` still advertises no DCR endpoint (2026-09-05), the monolith serves no RFC 9728 document. #3832, #3833 | shared with monolith, left in place |
