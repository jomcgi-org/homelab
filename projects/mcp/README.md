# MCP Gateway

The cluster deployment of **Context Forge**, the MCP (Model Context Protocol) gateway that aggregates cluster-internal tools for agents. It is the single in-cluster entry point that lets Claude Code, Claude.ai, and other MCP clients reach cluster services (SigNoz, ArgoCD, etc.) without per-service auth workarounds.

See [ADR 003](../../docs/decisions/agents/003-context-forge.md) for the original design rationale and [ADR 011](../../docs/decisions/agents/011-cloudflare-managed-oauth.md) for the Cloudflare Managed OAuth model that superseded an earlier in-cluster OAuth proxy. Two later decisions matter before changing anything here:

- [ADR 020](../../docs/decisions/agents/020-deprecate-context-forge-mcp-gateway.md) is **Accepted** and decides to delete Context Forge and serve MCP directly from the monolith. Execution is deferred and the live `mcp.jomcgi.dev` route is untouched, so everything below is still the deployed reality, but this component is on a path to removal (issues #3831, #3832, #3833).
- [ADR 055](../../docs/decisions/agents/055-tool-mediated-github-access.md) (Draft) makes this gateway the mediation point for agent GitHub access, which cuts against 020. That tension is unresolved and is called out in 055 rather than settled.

## What it is

Context Forge is the IBM [mcp-context-forge](https://github.com/IBM/mcp-context-forge) gateway, deployed as an ArgoCD Application in the `mcp` namespace. It exposes registered cluster-internal backends as virtual MCP tools over streamable HTTP. Agents never see raw backend credentials; the gateway injects them server-side.

External access (Claude.ai, Claude Code) goes through Cloudflare Tunnel to `mcp.jomcgi.dev`, where authentik is the IdP and Context Forge filters the catalogue per identity. In-cluster agents reach the gateway directly via ClusterIP.

The property the authorization model rests on is that **Context Forge's ACL is tool-granular**: it decides whether you may call `search_knowledge`, never what `search_knowledge` returns. Result-level scoping has to come from the backend, which is why the caller's token is forwarded to the monolith (see `ENABLE_HEADER_PASSTHROUGH` below) and why ADR 055 puts the target repo in each GitHub tool's URL: with a fixed tool surface, entitlement to a tool *is* the scope.

## Directory layout

```
projects/mcp/
└── context-forge-gateway/
    ├── chart/                  # Custom Helm chart wrapping the upstream mcp-stack subchart
    │   ├── Chart.yaml          # Chart metadata; declares mcp-stack + homelab-library deps
    │   ├── values.yaml         # Chart defaults (most feature flags off, auth config)
    │   └── templates/
    │       ├── _helpers.tpl        # Helm named template helpers
    │       ├── httproute.yaml      # Gateway API HTTPRoute for mcp.jomcgi.dev
    │       ├── networkpolicy.yaml  # Cross-namespace ingress policy (disabled; see below)
    │       ├── onepassworditem.yaml # 1Password secret sync (JWT_SECRET_KEY, etc.)
    │       └── tool-refresh-cronjob.yaml # Periodic re-registration of a gateway's tool catalog (enabled by default)
    └── deploy/
        ├── application.yaml    # ArgoCD Application (namespace: mcp, ServerSideApply)
        ├── kustomization.yaml  # Makes the app discoverable by the home-cluster root
        └── values.yaml         # Production overrides (resource limits, probes, hostname)
```

## How it builds and deploys

The chart wraps the upstream `mcp-stack` chart (mirrored to `ghcr.io/jomcgi/homelab/charts`). There is no custom container image in this repo; the gateway runs the upstream `ghcr.io/ibm/mcp-context-forge` image, pinned by tag in `chart/values.yaml`.

ArgoCD reads the chart directly from the Git repository path (`projects/mcp/context-forge-gateway/chart`) at HEAD and deep-merges `deploy/values.yaml` on top. No OCI chart push is needed for this service.

To update the bundled upstream chart version:

```bash
# Clone the desired tag, package, mirror to GHCR, then update Chart.yaml + re-run:
helm dependency update projects/mcp/context-forge-gateway/chart
```

## Key configuration notes

- **Auth:** read `deploy/values.yaml` for the effective values, never `chart/values.yaml`. Everything deliberately set lives in `mcpContextForge.extraEnv`, which outranks every `envFrom` source, and the two files disagree on purpose: `MCP_REQUIRE_AUTH` is `"false"` in the chart defaults and `"true"` in `deploy/values.yaml`, so the deployed value is **true** and the JSON-RPC endpoint authenticates. External-IdP bearer auth requires both `SSO_ENABLED` (mounts the `/auth/sso` router) and `SSO_API_TOKEN_AUTH_ENABLED` (lets a trusted provider's token authenticate an MCP request), plus a `trusted_for_api_auth` `sso_providers` row; no one of the three is sufficient alone. The `TRUST_PROXY_AUTH` / `PROXY_USER_HEADER: Cf-Access-Authenticated-User-Email` pair in `chart/values.yaml` dates from the Cloudflare Access era. Each `extraEnv` entry carries a comment explaining why it is set; read it before changing one.
- **Secrets:** A `OnePasswordItem` named `context-forge` (item path in `deploy/values.yaml`) syncs `JWT_SECRET_KEY`, `AUTH_ENCRYPTION_SECRET`, and `PLATFORM_ADMIN_EMAIL` into a Kubernetes secret. Backend API keys (SigNoz, ArgoCD) are also stored in 1Password and injected via `extraEnvFrom`.
- **NetworkPolicy disabled:** auth is enforced at the application layer instead.
- **In-cluster access:** `http://context-forge-gateway-mcp-stack-mcpgateway.mcp.svc.cluster.local:80/mcp`. This is no longer an unauthenticated path: `MCP_REQUIRE_AUTH` is gateway-wide rather than per virtual server, so in-cluster callers authenticate too. In-cluster registration jobs use a JWT (`MCP_CLIENT_AUTH_ENABLED`).
- **UI and catalog:** disabled by default in `chart/values.yaml`; re-enabled in `deploy/values.yaml` for the production cluster.
- **Tool catalog refresh:** `toolRefresh.enabled: true` by default (`chart/values.yaml`), running a CronJob every 10 minutes (`toolRefresh.schedule`). Context Forge caches each gateway's tool catalog in its own Postgres and does not re-discover it on its own: the built-in auto-refresh only fires after a healthy tick from the health-check loop, and that loop speaks streamable HTTP while the monolith gateway serves SSE, so its `last_seen` never advances and the auto-refresh never runs for it. The CronJob mints a short-lived admin JWT and POSTs `/gateways/{id}/tools/refresh` for the gateway named in `toolRefresh.gatewayName` (default `monolith`) so new or changed monolith MCP tools become visible without a manual refresh.

## Tool-mediated GitHub access (ADR 055, Draft, not built)

[ADR 055](../../docs/decisions/agents/055-tool-mediated-github-access.md) decides that agent principals get no direct GitHub egress. Instead:

- GitHub tools carry the target repo **in the tool's URL**, so an agent cannot construct an arbitrary GitHub request, only invoke the fixed set of tools it is entitled to.
- **Authentik groups gate those tools**, enforced by this gateway on both list and call.
- A **fine-grained PAT per group** sits in the tool headers, scoped to that group's repos and the permissions its tools need. The group gating and the PAT scope are two independent layers: a gating bug still cannot reach repos outside the group's PAT scope.

Two operational traps this creates, both worth checking before trusting the split:

- **Federation creates tools `visibility=public`.** A newly federated GitHub tool is visible to every authenticated caller until the reconcile pass runs. The tier map and reconcile pass are specified in issue #4569 and are a prerequisite, not a follow-up.
- **A group's tool entitlements and its PAT's repo scope must agree.** Entitle a group to a tool whose repo the PAT does not cover and the failure is a runtime 403, not a clean authorization denial. ADR 055 requires both to be generated from one declarative source for that reason; the two-copies coupling it is avoiding is the one recorded in `projects/firecracker/substrate/egress-proxy/cmd/swap.go:10-14`.
