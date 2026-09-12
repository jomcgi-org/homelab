# Services Overview

This document provides an overview of all services running in the cluster.

## Core Infrastructure (cluster-critical)

| Service                      | Purpose                                                                        | Location                                                                                                   |
| ---------------------------- | ------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------- |
| **ArgoCD**                   | GitOps controller for declarative cluster management                           | [projects/platform/argocd](../../projects/platform/argocd/)                                                   |
| **cert-manager**             | X.509 certificate management for in-cluster TLS                     | [projects/platform/cert-manager](../../projects/platform/cert-manager/)                                       |
| **CoreDNS**                  | Cluster DNS resolution for Kubernetes services                                 | [projects/platform/coredns](../../projects/platform/coredns/)                                                 |
| **Kyverno**                  | Policy engine for admission, resource audits, and secret replication   | [projects/platform/kyverno](../../projects/platform/kyverno/)                                                 |
| **Cilium**                   | eBPF CNI: WireGuard pod-to-pod encryption, network policy, Hubble metrics      | [projects/platform/cilium](../../projects/platform/cilium/)                                                   |
| **Longhorn**                 | Distributed persistent storage with automated backups                          | [projects/platform/longhorn](../../projects/platform/longhorn/)                                               |
| **NVIDIA GPU Operator**      | GPU support for LLM inference workloads                                        | [projects/platform/nvidia-gpu-operator](../../projects/platform/nvidia-gpu-operator/)                         |
| **otel-collector**            | Deny-by-default trace collector and public URL probes exporting to Honeycomb   | [projects/platform/otel-collector](../../projects/platform/otel-collector/)                                   |
| **OpenTelemetry Operator**   | OpenTelemetry auto-instrumentation operator; language injection is disabled    | [projects/platform/opentelemetry-operator](../../projects/platform/opentelemetry-operator/)                   |
| **Argo Workflows**           | Namespace-scoped batch-job executor for monolith workflows                     | [projects/platform/argo-workflows](../../projects/platform/argo-workflows/)                                   |
| **Atlas Operator**           | Declarative database schema migrations via Atlas CRDs                          | [projects/platform/atlas-operator](../../projects/platform/atlas-operator/)                                   |
| **CloudNativePG**            | PostgreSQL operator for in-cluster databases                                   | [projects/platform/cloudnative-pg](../../projects/platform/cloudnative-pg/)                                   |
| **KEDA**                     | Event-driven autoscaler, shared infrastructure                                 | [projects/platform/keda](../../projects/platform/keda/)                                                       |
| **Node Traffic Shaper**      | Caps inbound node bandwidth with CAKE to protect control-plane traffic         | [projects/platform/node-traffic-shaper](../../projects/platform/node-traffic-shaper/)                         |
| **1Password Operator**       | Secret management via OnePasswordItem CRDs                                     | External chart (Helm install, outside ArgoCD)                                                              |

## Production Services (prod)

| Service                | Purpose                                     | Location                                                                         |
| ---------------------- | ------------------------------------------- | -------------------------------------------------------------------------------- |
| **Cloudflare Gateway** | Zero Trust ingress (no open firewall ports) | [projects/platform/cloudflare-gateway](../../projects/platform/cloudflare-gateway/) |
| **SeaweedFS**          | Distributed S3-compatible object storage    | [projects/platform/seaweedfs](../../projects/platform/seaweedfs/)                   |
| **SeaweedFS node-4**   | Second SeaweedFS volume server on node-4 for replication | [projects/platform/seaweedfs-node4](../../projects/platform/seaweedfs-node4/) |
| **Monolith**           | Primary application backend and frontend    | [projects/monolith](../../projects/monolith/)                                       |
| **Monolith Public**    | Read-only public tier serving jomcgi.dev    | [projects/monolith-public](../../projects/monolith-public/)                         |
| **EmberVM**            | Firecracker microVM orchestration for agent sandboxes | [projects/embervm](../../projects/embervm/)                               |
| **Inference**          | Self-hosted LLM inference (vLLM)            | [projects/inference](../../projects/inference/)                                     |
| **Context Forge Gateway** | MCP gateway in front of the monolith tool surface for Claude.ai and Claude Code | [projects/mcp/context-forge-gateway](../../projects/mcp/context-forge-gateway/) |

## Development Services (dev)

| Service             | Purpose                             | Location                                                                     |
| ------------------- | ----------------------------------- | ---------------------------------------------------------------------------- |
| **Grimoire**        | D&D corpus, graph, campaigns, and retrieval | [Monolith Grimoire](../../projects/monolith/grimoire/architecture.md)       |
| **OCI Model Cache** | HuggingFace model caching operator  | [projects/operators/oci-model-cache](../../projects/operators/oci-model-cache/) |

## Public Web

The public website (apex `jomcgi.dev`, including `/docs`, the `/app/*` apps, and
the CV) is served by the monolith's read-only public tier, not standalone static
sites. See [monolith-public](../../projects/monolith-public/) and the
[monolith frontend](../../projects/monolith/frontend/). The old Astro/VitePress
Cloudflare Pages frontends were decommissioned (ADR docs/002).

### Public response cache contract

The shared Cloudflare cache contract applies only to public, anonymous,
cookie-free responses covered by the `jomcgi.dev` apex hostname Cache Rule.
A cacheable response must not depend on `Authorization`, a session, cookies, or
any other caller-specific state, must not contain personalized data, and must
not set a cookie. Authenticated or personalized responses are outside this
contract and must not be marked `public` for shared caching.

Every new public data route must set its cache policy explicitly at the
internet-facing response. For a SvelteKit same-origin proxy, select the
route-specific constant from
[`cache-headers.js`](../../projects/monolith/frontend/src/lib/cache-headers.js)
and pass it through `cloudflareCacheHeaders()`. Do not rely on a backend header
surviving the proxy, on a Cloudflare default TTL, or on the hostname rule to
invent a policy. Keep a mirrored backend policy synchronized when the backend
also emits the header.

The stats proxy is the canonical short-lived data example. Its policy is:

```http
Cache-Control: public, max-age=0, s-maxage=60, stale-while-revalidate=86400, stale-if-error=31536000
Cloudflare-CDN-Cache-Control: public, max-age=60, stale-while-revalidate=86400, stale-if-error=31536000
```

The two headers deliberately address different caches. `max-age=0` in
`Cache-Control` makes a browser revalidate on each use instead of retaining a
stale JSON snapshot. `s-maxage=60` gives shared caches a 60-second lifetime.
`cloudflareCacheHeaders()` converts that shared lifetime to `max-age=60` in the
higher-precedence Cloudflare-only header because Cloudflare treats `s-maxage`
as `proxy-revalidate`, which would disable the stale directives. See the
canonical use in the
[`/app/notes/stats` proxy](../../projects/monolith/frontend/src/routes/public/app/notes/stats/+server.js)
and its mirrored backend value in
[`home/observability/router.py`](../../projects/monolith/home/observability/router.py).

Default a new public data poller to the existing five-minute cadence in
[`homepage-stats.js`](../../projects/monolith/frontend/src/routes/public/homepage-stats.js),
then choose a different interval only when the data's refresh rate and user
experience justify it. The Notes app's 20-second live readout is an existing
route-specific example: browser revalidation occurs on every poll, while the
60-second shared lifetime lets the edge absorb those requests. Browser and edge
freshness are separate decisions, so never use browser caching as a substitute
for a polling interval.

These headers establish the code-side contract, not proof that the live edge
accepted the response. After deployment, verify repeated requests on the
hostname covered by the rule and inspect `cf-cache-status` and `age`. Keep a
route's live cache acceptance recorded as unverified until that check succeeds.
The [public-tier checklist](../runbooks/public-tier-checklist.md) carries the
implementation and rollout checks.

## Service Details

The monolith drainer claims `kg-drain` routine jobs alongside `qwen-drain`
jobs. Each `kg-drain` payload carries a `raw_id` for the selected knowledge raw
input. After Luna completes the extraction turn, the monolith validates the
result and writes the resulting atoms itself. Drainer failures surface through
the `drainer` and `kg` health advisories and warning logs. Per-job Discord
failure notifications are off by default and can be enabled with
`agents.drainer.notifyFailures`.

Agent sessions bound to Discord threads always post turn output back to their
thread. Thread-less sessions post only turns needing human input to the
agent-session channel by default. `agents.sessions.channelNotify` can select
`needs-input`, `all`, or `none`.

For detailed information about specific services, see the README in each project directory:

- `projects/<service>/README.md`
- `projects/platform/<service>/README.md`
