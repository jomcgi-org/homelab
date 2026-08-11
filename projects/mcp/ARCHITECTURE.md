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

## Identity decides which tools you see

Entitlement is expressed as **tags on tools**, not as a server or a URL per
audience. `list_server_tools` filters per caller from `tools.visibility` and
Context Forge team membership, so one virtual server (`homelab-admin`) can carry
the full monolith catalogue and still show each caller only their subset.

Group matching runs against the authentik `groups` claim by **name**, so
renaming a group in authentik silently stops matching. The names are not ids.

The ACL is **tool-granular**: it decides whether you may call
`search_knowledge`, never what `search_knowledge` returns. Per-caller scoping of
results is a separate concern, and is not live (see Token forwarding).

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

**This is inert until the monolith validates the token.** Per-caller data
scoping therefore does not work today. The token's `aud` is the MCP client id
rather than the monolith, so the monolith must validate `iss` strictly and
knowingly accept that audience. RFC 8693 token exchange is the correct fix and
Context Forge implements it, but authentik does not advertise the endpoint.

## Deployment

**This service deploys from a git path at `targetRevision: HEAD`, not from an
OCI chart.** That is a deliberate exception to the pattern documented for new
services. ArgoCD reads `projects/mcp/context-forge-gateway/chart` directly and
deep-merges `deploy/values.yaml` on top.

The practical consequence: **merging deploys instantly**, and there is no chart
version bump in the loop. Nothing waits for a `chart-version-bot` write-back.

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

## Tool catalogue refresh

A CronJob mints a short-lived admin JWT and POSTs
`/gateways/{id}/tools/refresh` every 10 minutes.

It exists because Context Forge caches each gateway's tool catalogue in its own
Postgres and does not rediscover on its own. The built-in auto-refresh only
fires after a healthy tick from the health-check loop, and that loop speaks
streamable HTTP while the monolith gateway serves SSE. So the monolith's
`last_seen` never advances and its auto-refresh never runs.

## State

Postgres via CloudNativePG (`templates/postgres-cnpg.yaml`). Context Forge holds
its own gateway registrations, team membership, tool catalogue cache and
`sso_providers` there. Secrets sync from 1Password via `OnePasswordItem`.

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
