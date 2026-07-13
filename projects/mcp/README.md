# MCP Gateway

The cluster deployment of **Context Forge**, the MCP (Model Context Protocol) gateway that aggregates cluster-internal tools for agents. It is the single in-cluster entry point that lets Claude Code, Claude.ai, and other MCP clients reach cluster services (SigNoz, ArgoCD, etc.) without per-service auth workarounds.

See [ADR 003](../../docs/decisions/agents/003-context-forge.md) for the original design rationale and [ADR 011](../../docs/decisions/agents/011-cloudflare-managed-oauth.md) for the current auth model (Cloudflare Managed OAuth superseded an earlier in-cluster OAuth proxy).

## What it is

Context Forge is the IBM [mcp-context-forge](https://github.com/IBM/mcp-context-forge) gateway, deployed as an ArgoCD Application in the `mcp` namespace. It exposes registered cluster-internal backends as virtual MCP tools over streamable HTTP. Agents never see raw backend credentials; the gateway injects them server-side.

External access (Claude.ai, Claude Code) goes through Cloudflare Tunnel to `mcp.jomcgi.dev`. Cloudflare Access Managed OAuth handles authentication at the edge. In-cluster agents reach the gateway directly via ClusterIP.

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

- **Auth:** `TRUST_PROXY_AUTH=true` with `PROXY_USER_HEADER: Cf-Access-Authenticated-User-Email`. Cloudflare Access validates tokens at the edge before traffic reaches the pod; the gateway trusts the injected identity header.
- **Secrets:** A `OnePasswordItem` named `context-forge` (item path in `deploy/values.yaml`) syncs `JWT_SECRET_KEY`, `AUTH_ENCRYPTION_SECRET`, and `PLATFORM_ADMIN_EMAIL` into a Kubernetes secret. Backend API keys (SigNoz, ArgoCD) are also stored in 1Password and injected via `extraEnvFrom`.
- **NetworkPolicy disabled:** auth is enforced at the application layer instead.
- **In-cluster access:** `http://context-forge-gateway-mcp-stack-mcpgateway.mcp.svc.cluster.local:80/mcp` (no OAuth required for in-cluster callers).
- **UI and catalog:** disabled by default in `chart/values.yaml`; re-enabled in `deploy/values.yaml` for the production cluster.
- **Tool catalog refresh:** `toolRefresh.enabled: true` by default (`chart/values.yaml`), running a CronJob every 10 minutes (`toolRefresh.schedule`). Context Forge caches each gateway's tool catalog in its own Postgres and does not re-discover it on its own: the built-in auto-refresh only fires after a healthy tick from the health-check loop, and that loop speaks streamable HTTP while the monolith gateway serves SSE, so its `last_seen` never advances and the auto-refresh never runs for it. The CronJob mints a short-lived admin JWT and POSTs `/gateways/{id}/tools/refresh` for the gateway named in `toolRefresh.gatewayName` (default `monolith`) so new or changed monolith MCP tools become visible without a manual refresh.
