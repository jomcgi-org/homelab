# RFC: MCP Context Forge as Agent Tool Gateway

**Author:** Joe McGinley
**Status:** Superseded by [020 - Deprecate Context Forge](020-deprecate-context-forge-mcp-gateway.md)
**Created:** 2026-02-27

---

## Problem

Agents (Claude Code, Cursor, OpenHands sandboxes) need programmatic access to cluster-internal services — SigNoz, ArgoCD, Longhorn, the K8s API — but every service is behind Cloudflare Zero Trust SSO. Today this creates three problems:

1. **Auth fragmentation** — Each MCP server needs its own Cloudflare bypass. The SigNoz MCP server (`github.com/SigNoz/signoz-mcp-server`) doesn't support `CF-Access-Client-Id`/`CF-Access-Client-Secret` headers. Neither do most third-party MCP servers. The workaround is a local proxy per service, which doesn't scale.

2. **Bash pattern sprawl** — `settings.local.json` has 460+ permission patterns. Agents construct CLI commands from memory, parse unstructured stdout, and handle errors ad-hoc. Each new tool requires new Bash patterns and custom error handling.

3. **No shared access for remote agents** — Local Claude Code sessions can use stdio-based MCP servers, but OpenHands sandbox pods in the cluster cannot. Any MCP server that runs as a local subprocess is invisible to remote agents.

---

## Proposal

Deploy IBM MCP Context Forge (Apache 2.0) as a single in-cluster gateway that wraps internal REST/HTTP APIs as virtual MCP tools via configuration. The gateway runs inside the cluster network, accessing backends via ClusterIP directly. Agents connect through Cloudflare Zero Trust using a service token — no port-forwarding, no local proxies, no per-MCP-server auth workarounds.

### Before and After

| Aspect                | Today                                    | With Context Forge                                         |
| --------------------- | ---------------------------------------- | ---------------------------------------------------------- |
| SigNoz access         | Custom MCP binary + orphaned permissions | `signoz.query_logs({service: "trips", severity: "ERROR"})` |
| ArgoCD access         | `argocd app get` via Bash pattern        | `argocd.get_application({name: "trips"})`                  |
| Cloudflare auth       | Per-MCP-server workaround needed         | Gateway handles it once, backends are cluster-internal     |
| Remote agent access   | Not possible (stdio only)                | HTTP transport, any agent in or outside cluster            |
| New tool provisioning | Build MCP server + add Bash patterns     | Register API endpoint in gateway config                    |

---

## Architecture

```
┌──────────────────────────────────┐
│  Agents                          │
│  - Claude Code (local)           │
│  - OpenHands sandboxes (cluster) │
│  - Cursor                        │
└───────────────┬──────────────────┘
                │
        ┌───────┴────────────────────────────────┐
        │ Local agents:                          │  In-cluster agents:
        │ HTTPS to mcp.jomcgi.dev                │  ClusterIP (no auth)
        │ + CF service token headers             │
        └───────┬────────────────────────────────┘
                │
                ▼
    ┌─ Cloudflare Zero Trust ─┐
    │  mcp.jomcgi.dev         │
    │  Service token policy   │
    └───────────┬─────────────┘
                │
                ▼
┌─ Namespace: mcp-gateway ──────────────────────────────────┐
│                                                            │
│  Deployment: context-forge (1 replica)                     │
│  ├─ Registers virtual MCP tools for each backend           │
│  ├─ Transport: streamable-HTTP                             │
│  ├─ Built-in: rate limits, OTel traces, request logging    │
│  └─ ClusterIP Service on port 8000                         │
│                                                            │
│  Backends (cluster-internal, no Cloudflare):               │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐  │
│  │ SigNoz   │ │ ArgoCD   │ │ Longhorn │ │ K8s API      │  │
│  │ :8080    │ │ :80      │ │ :80      │ │ (via SA)     │  │
│  └──────────┘ └──────────┘ └──────────┘ └──────────────┘  │
│                                                            │
│  OnePasswordItem: mcp-gateway-secrets                      │
│  ├─ SIGNOZ_API_KEY                                         │
│  └─ ARGOCD_AUTH_TOKEN                                      │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

### How Cloudflare Is Bypassed

The gateway pod runs inside the cluster network. It accesses backends via their ClusterIP services directly:

- SigNoz: `http://signoz.signoz.svc.cluster.local:8080`
- ArgoCD: `http://argocd-server.argocd.svc.cluster.local:80`
- Longhorn: `http://longhorn-frontend.longhorn.svc.cluster.local:80`

No Cloudflare tunnel, no CF headers, no SSO. The gateway itself is the auth boundary — it's exposed to external agents via a Cloudflare tunnel route at `mcp.jomcgi.dev`, where Zero Trust handles authentication before traffic reaches the pod.

This is the same pattern used by every other service in the cluster (see `overlays/prod/cloudflare-tunnel/values.yaml`).

### Agent Access: Cloudflare Service Token

All external agents connect via `https://mcp.jomcgi.dev/mcp` using Cloudflare Access service token headers. Claude Code natively supports custom headers on remote MCP servers via the `.mcp.json` `headers` field:

```json
{
  "mcpServers": {
    "context-forge": {
      "type": "http",
      "url": "https://mcp.jomcgi.dev/mcp",
      "headers": {
        "CF-Access-Client-Id": "${CF_ACCESS_CLIENT_ID}",
        "CF-Access-Client-Secret": "${CF_ACCESS_CLIENT_SECRET}"
      }
    }
  }
}
```

The `${VAR}` syntax expands environment variables at runtime — secrets stay in the local shell environment (e.g., via `direnv` and 1Password CLI) rather than in the config file checked into Git.

This reuses the same service token pattern the cluster's OTel deployment already uses for synthetic health checks (the `synthetic-tests` 1Password item). A dedicated service token scoped to `mcp.jomcgi.dev` can be created for tighter access control.

**In-cluster agents** (OpenHands sandboxes) access the gateway via ClusterIP: `http://context-forge.mcp-gateway.svc.cluster.local:8000/mcp`. No Cloudflare auth needed — ClusterIP is not externally reachable, and sandboxes are already scoped to an isolated namespace with ResourceQuota (see `docs/decisions/agents/002-openhands-agent-sandbox.md`).

### Full Request Flow

```
Claude Code (local Mac)
    │
    │  HTTPS + CF-Access-Client-Id + CF-Access-Client-Secret
    │
    ▼
Cloudflare Zero Trust (mcp.jomcgi.dev)
    │  ✓ Service token validated
    │  ✓ No browser SSO required
    ▼
Cloudflare Tunnel → context-forge.mcp-gateway.svc.cluster.local:8000
    │
    ▼
Context Forge pod → SigNoz / ArgoCD (ClusterIP, backend credentials injected server-side)
```

No port-forwarding. No local proxy. No exposed ports. The service token bypasses interactive SSO while still being validated by Zero Trust.

---

## MVP: SigNoz + ArgoCD (read-only)

Deploy Context Forge with two backends — the two services agents query most frequently. Read-only operations only.

### What the MVP Proves

- Agents can query SigNoz logs/traces/metrics without a custom MCP binary or Cloudflare bypass
- Agents can inspect ArgoCD application state without `argocd` CLI + Bash patterns
- HTTP transport works from both local Claude Code and in-cluster OpenHands sandboxes
- OTel spans flow through the existing Kyverno-injected collector to SigNoz (observing the observer)

### MVP Virtual Tools

**SigNoz** (read-only):

| Tool                   | Backend Endpoint               | Purpose                               |
| ---------------------- | ------------------------------ | ------------------------------------- |
| `signoz.list_services` | `GET /api/v1/services`         | Discover services                     |
| `signoz.search_logs`   | `POST /api/v3/query_range`     | Query logs by service/severity        |
| `signoz.search_traces` | `POST /api/v3/query_range`     | Find traces by service/error/duration |
| `signoz.get_trace`     | `GET /api/v1/traces/{traceId}` | Full trace with spans                 |
| `signoz.list_alerts`   | `GET /api/v1/rules`            | Active alert rules                    |

**ArgoCD** (read-only):

| Tool                       | Backend Endpoint                          | Purpose                        |
| -------------------------- | ----------------------------------------- | ------------------------------ |
| `argocd.list_applications` | `GET /api/v1/applications`                | All managed apps               |
| `argocd.get_application`   | `GET /api/v1/applications/{name}`         | App status, sync state, health |
| `argocd.get_app_history`   | `GET /api/v1/applications/{name}/history` | Deployment history             |

### MVP Constraints

**Auth:** Read-only service accounts only. SigNoz viewer API key, ArgoCD read-only token. No write verbs registered in the gateway.

**Network:** ClusterIP service. External access via Cloudflare tunnel route (`mcp.jomcgi.dev`). Zero Trust policy requires a CF service token — created in Cloudflare Zero Trust dashboard (Access > Service Auth > Service Tokens), scoped to the `mcp.jomcgi.dev` application.

**SSRF:** Context Forge defaults block private network ranges. Required configuration:

```
SSRF_ALLOWED_NETWORKS=["10.42.0.0/16", "10.43.0.0/16"]
```

(Adjust to match cluster pod and service CIDRs.)

**Secrets:** All credentials via `OnePasswordItem` CRDs, consistent with every other service. No secrets in Git or container images. The CF service token for local agent access is stored in 1Password and loaded into the shell environment via `direnv` + 1Password CLI.

**Container security:** Standard homelab security context — non-root (uid 65532), read-only root filesystem, drop all capabilities.

### What the MVP Does NOT Cover

- Write operations (ArgoCD sync, SigNoz dashboard creation)
- K8s API registration (would require ServiceAccount with cluster read access)
- Longhorn API registration
- Virtual server bundling (role-scoped tool subsets per agent type)
- JWT-based per-agent authentication

---

## GitOps Structure

Following the standard homelab pattern:

```
charts/context-forge/           # Helm chart
  Chart.yaml
  values.yaml
  templates/
    deployment.yaml
    service.yaml
    configmap.yaml              # Gateway config (registered backends)

overlays/prod/context-forge/    # ArgoCD Application
  application.yaml
  kustomization.yaml
  values.yaml                   # Production overrides
```

The gateway configuration (which backends to register, which endpoints to expose) lives in the ConfigMap. Adding a new tool is a values.yaml change — commit, push, ArgoCD syncs.

---

## Security Model

### Trust Boundaries

```
Internet ──[Cloudflare Zero Trust]──▶ mcp.jomcgi.dev ──▶ Context Forge pod
             (service token)                                    │
                                                                ├──▶ SigNoz (viewer key)
                                                                └──▶ ArgoCD (read-only token)
```

- **External agents** (local Claude Code, Cursor): authenticated by Cloudflare Zero Trust via service token before traffic reaches the cluster. Same trust model as `signoz.jomcgi.dev`, `argocd.jomcgi.dev` — nothing is exposed outside of Zero Trust.
- **In-cluster agents** (OpenHands sandboxes): access via ClusterIP. No Cloudflare auth needed — but sandboxes are already scoped to an isolated namespace with ResourceQuota (see `docs/decisions/agents/002-openhands-agent-sandbox.md`).
- **Gateway → backends**: uses service-specific read-only credentials. The gateway pod holds these secrets; agents never see raw API keys.

### What the Gateway Does NOT Do

- **No write operations** in MVP. The gateway registers only read endpoints. Write verbs (ArgoCD sync, SigNoz dashboard mutation) are future work requiring explicit scoping.
- **No credential forwarding** to agents. Agents call virtual MCP tools; the gateway injects backend credentials server-side. Agents never see SigNoz API keys or ArgoCD tokens.
- **No cluster-admin access.** Backend tokens are scoped to specific API surfaces (SigNoz viewer, ArgoCD read-only).

### Deviations from Security Model

None. This deployment follows all five layers from `docs/security.md`:

- Non-root, read-only filesystem, drop all capabilities
- Linkerd-meshed (mTLS to backends)
- Kyverno-validated
- Secrets via 1Password
- Ingress via Cloudflare only

---

## Risks

| Risk                                                                               | Mitigation                                                                                                                                                               |
| ---------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Gateway SPOF** — pod failure blocks all MCP tool access                          | Single replica acceptable for homelab. Agents fall back to direct CLI/Bash patterns if gateway is down. Add PDB if promoting to multi-replica.                           |
| **Upstream maturity** — no GA `1.0.0` yet                                          | Pin to specific release tag. 30-day checkpoint: if upstream goes dormant, the gateway config is portable — backends are standard REST APIs that any MCP server can wrap. |
| **SSRF via registered endpoints** — attacker-controlled input reaches backend APIs | Read-only endpoints only. SSRF allowlist restricts to cluster CIDRs. Gateway validates request schemas before forwarding.                                                |
| **Credential exposure** — gateway pod compromise exposes backend tokens            | Tokens are read-only scoped. Rotate via 1Password. Pod runs with standard security context (non-root, read-only fs, no privilege escalation).                            |

---

## Rollout

### Phase 1 — MVP Gateway

- Deploy Context Forge as ArgoCD Application
- Register SigNoz (viewer key) and ArgoCD (read-only token) as virtual tools
- Cloudflare tunnel route: `mcp.jomcgi.dev` → `context-forge.mcp-gateway.svc.cluster.local:8000`
- Create CF service token scoped to `mcp.jomcgi.dev` in Zero Trust dashboard
- Store CF service token in 1Password, configure local `direnv` to export `CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET`
- Add `.mcp.json` to the repo with the gateway config (env var refs, no secrets)
- Validate from local Claude Code session
- Validate from OpenHands sandbox pod (if deployed)
- **Success criteria:** agent queries SigNoz logs and ArgoCD app status via MCP tools, no Bash patterns or port-forwarding needed

### Phase 2 — Expand Backends

- Register Longhorn API (volume status, backup info)
- Register K8s API subset (pod list, node status — via ServiceAccount with scoped RBAC)
- Add OTel traces for gateway requests to SigNoz

### Phase 3 — Agent Role Scoping

- Virtual server bundling: infrastructure agents get all tools, development agents get SigNoz + ArgoCD only
- Per-agent JWT auth (if multi-tenant agent access is needed)
- Write operations for specific tools (ArgoCD sync trigger with approval gate)

---

## Open Questions

1. **Namespace placement** — `mcp-gateway` (dedicated) or colocate with `api-gateway`? Dedicated namespace is cleaner for RBAC scoping if K8s API access is added in Phase 2.

2. **Upstream version pinning** — `0.9.0` (last stable) or `1.0.0rc1` (with SSRF allowlist)? RC1 has the SSRF controls we need but may have breaking changes.

3. **Container image** — Context Forge is Python/PyPI. Build an apko image with the package, or use upstream Docker image? Apko is consistent with the repo but requires maintaining a Python layer.

---

## References

| Resource                                                                                                                          | Relevance                                                                  |
| --------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| [IBM MCP Context Forge](https://github.com/IBM/mcp-context-forge)                                                                 | Gateway source, Apache 2.0                                                 |
| [Cloudflare Service Tokens](https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/) | Service token creation and scoping                                         |
| [Claude Code MCP Configuration](https://docs.anthropic.com/en/docs/claude-code/mcp)                                               | `.mcp.json` format, `headers` field, env var expansion                     |
| [SigNoz API docs](https://signoz.io/docs/developers/query-service/)                                                               | Backend API surface for virtual tool registration                          |
| [ArgoCD API docs](https://cd.apps.argoproj.io/swagger-ui)                                                                         | Backend API surface for virtual tool registration                          |
| [docs/security.md](../../security.md)                                                                                             | Cluster security model (this RFC is fully compliant)                       |
| [agents/002 - OpenHands agent sandbox](002-openhands-agent-sandbox.md)                                                        | OpenHands sandbox architecture (in-cluster agent consumer of this gateway) |
