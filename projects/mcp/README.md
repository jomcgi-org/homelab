# MCP Gateway

The cluster deployment of **Context Forge**, the MCP (Model Context Protocol) gateway in front of the monolith's tool surface. It is the entry point that lets Claude.ai connectors and Claude Code reach the monolith's tools with one OAuth login, and it filters that catalogue per caller identity.

**How the whole surface fits together, including the parts that are not in this directory: [ARCHITECTURE.md](ARCHITECTURE.md).** That is the current-state document and the thing to link to. This README stays the operator's view of the deployment.

Two facts to hold before changing anything here:

- Context Forge is one of **two** MCP entry points. Ember guests never reach it; they talk to the monolith-agents tier through the egress sidecar. See [Topology](ARCHITECTURE.md#topology).
- Its removal is decided but not executed. ADR agents/059 (Draft) supersedes agents/020 and keeps the same end state, the monolith serving `mcp.jomcgi.dev` directly; #3832 (cutover) and #3833 (decommission) carry the work, and everything below is still the deployed reality. The [decision history](ARCHITECTURE.md#decision-history) preserves that rationale.

## What it is

Context Forge is the IBM [mcp-context-forge](https://github.com/IBM/mcp-context-forge) gateway, deployed as an ArgoCD Application in the `mcp` namespace of the GKE hub. It exposes registered backends as virtual MCP tools over streamable HTTP. Two backends are registered and only one is enabled: the monolith's `/mcp` mount. The GitHub registration is disabled and carries no tools.

External access (Claude.ai, Claude Code) goes through Cloudflare Tunnel to `mcp.jomcgi.dev`, where authentik is the IdP and Context Forge filters the catalogue per identity. The only in-cluster caller is the chart's own team-mapping CronJob, which uses the REST API; in-cluster agents do not use this gateway.

The property the authorization model rests on is that **Context Forge's ACL is tool-granular**: it decides whether you may call `search_knowledge`, never what `search_knowledge` returns. Result-level scoping has to come from the backend, which is why the caller's token is forwarded to the monolith (see [Token forwarding](ARCHITECTURE.md#token-forwarding-to-upstreams-and-what-is-not-live-yet)).

## Directory layout

```
projects/mcp/
└── context-forge-gateway/
    ├── chart/                  # Custom Helm chart wrapping the upstream mcp-stack subchart
    │   ├── Chart.yaml          # Chart metadata; declares mcp-stack + homelab-library deps
    │   ├── values.yaml         # Chart defaults (feature flags off, auth flags, inert teamMapping)
    │   ├── files/reconcile_team_mapping.py   # Team-mapping reconciler run by the CronJob
    │   └── templates/
    │       ├── httproute.yaml            # mcp.jomcgi.dev: RFC 9728 discovery + /mcp rewrite
    │       ├── httproute-scoped.yaml     # Per-tier hostnames (friends.jomcgi.dev), allowlist routes
    │       ├── httproute-preview.yaml    # Browser-authenticated preview lane, not MCP
    │       ├── team-mapping-cronjob.yaml # Reconciles authentik group -> CF team from values
    │       ├── postgres-cnpg.yaml        # CloudNativePG cluster and scheduled GCS backup
    │       ├── cnpg-backup-gcs-secret.yaml
    │       ├── onepassworditem.yaml      # 1Password secret sync (JWT_SECRET_KEY, etc.)
    │       └── networkpolicy.yaml        # Cross-namespace ingress policy (disabled; see below)
    ├── scripts/provision-mcp-auth.sh     # Creates the sso_providers row after a DB rebuild
    └── deploy/
        ├── application.yaml    # Home-cluster Application shape (no root references it)
        ├── kustomization.yaml
        ├── values.yaml         # Production overrides (extraEnv auth flags, routes, teams)
        ├── values-gke.yaml     # Hub overrides (CNPG recovery bootstrap, storage class)
        └── cnpg-gcs-backup-secret.md
```

The hub's Application lives in `projects/gke-apps/context-forge-gateway/` and layers the three values files in that order.

## How it builds and deploys

The chart wraps the upstream `mcp-stack` chart (mirrored to `ghcr.io/jomcgi/homelab/charts`). There is no custom container image in this repo; the gateway runs the upstream `ghcr.io/ibm/mcp-context-forge` image, pinned by tag in `chart/values.yaml`.

ArgoCD reads the chart directly from the Git repository path (`projects/mcp/context-forge-gateway/chart`) at HEAD and deep-merges `deploy/values.yaml` and `deploy/values-gke.yaml` on top. No OCI chart push is needed for this service: merging is the deploy.

To update the bundled upstream chart version:

```bash
# Clone the desired tag, package, mirror to GHCR, then update Chart.yaml + re-run:
helm dependency update projects/mcp/context-forge-gateway/chart
```

## Key configuration notes

- **Auth:** read `deploy/values.yaml` for the effective values, never `chart/values.yaml`. Everything deliberately set lives in `mcpContextForge.extraEnv`, which outranks every `envFrom` source, and the two files disagree on purpose: `MCP_REQUIRE_AUTH` is `"false"` in the chart defaults and `"true"` in `deploy/values.yaml`, so the deployed value is **true** and the JSON-RPC endpoint authenticates. External-IdP bearer auth requires both `SSO_ENABLED` (mounts the `/auth/sso` router) and `SSO_API_TOKEN_AUTH_ENABLED` (lets a trusted provider's token authenticate an MCP request), plus a `trusted_for_api_auth` `sso_providers` row; no one of the three is sufficient alone. Each `extraEnv` entry carries a comment explaining why it is set; read it before changing one. The full effective table is in [Authentication](ARCHITECTURE.md#authentication).
- **Secrets:** A `OnePasswordItem` named `context-forge` (item path in `deploy/values.yaml`) syncs `JWT_SECRET_KEY`, `AUTH_ENCRYPTION_SECRET`, `PLATFORM_ADMIN_EMAIL` and `PLATFORM_ADMIN_PASSWORD` into a Kubernetes secret. No backend credential is injected: the monolith registration has no gateway-level auth, which is what lets the caller's own token pass through.
- **NetworkPolicy disabled:** auth is enforced at the application layer instead.
- **In-cluster access:** the gateway Service's `/mcp` path inside the `mcp` namespace (the hostname is whatever the release renders; read it from the Service, never hardcode it). This is no longer an unauthenticated path: `MCP_REQUIRE_AUTH` is gateway-wide rather than per virtual server, so in-cluster callers authenticate too. The one in-cluster caller, the team-mapping CronJob, mints a short-lived admin JWT from the synced secret and calls the REST API, not `/mcp`.
- **UI and catalog:** disabled by default in `chart/values.yaml`; re-enabled in `deploy/values.yaml` for the production cluster.
- **Tool catalog refresh:** The monolith registration lives in Context Forge's Postgres, not git, and is set by hand to `STREAMABLEHTTP`. Context Forge's health-check loop is now the only catalog refresh path, with `AUTO_REFRESH_SERVERS=true` and `GATEWAY_AUTO_REFRESH_INTERVAL=600`. Failure reporting is warning only, and a tool rejected by the XSS validator is dropped silently, so check `last_refresh_at` on the monolith `gateways` row and compare the tool count. Refresh never publishes a new tool: the association and visibility rows are hand edits, listed in [Tool catalogue refresh](ARCHITECTURE.md#tool-catalogue-refresh).
- **SSRF allowlist:** `SSRF_ALLOWED_NETWORKS` is checked when a gateway row is created or updated, so it only bites on the next registration. It still names the home cluster's ranges; see [Configuration discipline](ARCHITECTURE.md#configuration-discipline).

## GitHub access for agents

ADR agents/055 put GitHub tool mediation on this gateway. ADR agents/059 superseded it: mediation moves to the monolith's own GitHub mutation broker (#4946), enforced against a `Principal`'s delegation rather than a group's PAT, and nothing of 055's design was built here. The GitHub gateway registration in Context Forge is disabled and carries no tools. Agent guests reach GitHub through the EmberVM egress lane, which is that domain's concern.
