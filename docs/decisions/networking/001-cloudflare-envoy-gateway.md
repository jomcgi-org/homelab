# ADR 001: Reduce Cloudflare Operator Scope via Envoy Gateway

**Author:** Joe McGinley
**Status:** Implemented
**Created:** 2026-02-28
**Relates to:** K8s API Tunnel RFC

---

## Problem

The Cloudflare operator (`operators/cloudflare/`) has grown to 6 controllers and 2 CRDs:

| Controller               | Responsibility                                                                            |
| ------------------------ | ----------------------------------------------------------------------------------------- |
| `GatewayClass`           | Validates credentials, sets Accepted condition                                            |
| `Gateway`                | Creates `CloudflareTunnel` CRD, deploys `cloudflared` Deployment + HPA + PDB              |
| `CloudflareTunnel`       | Full state machine for Cloudflare tunnel API lifecycle (create/delete/credentials)        |
| `HTTPRoute`              | Resolves backends, creates Cloudflare published routes + DNS CNAME records                |
| `CloudflareAccessPolicy` | Manages Cloudflare Zero Trust access applications (GEP-713 policy attachment)             |
| `Service`                | Convenience layer — annotation-driven auto-creation of Gateway + HTTPRoute + AccessPolicy |

This reimplements routing primitives (listener config, backend resolution, route matching) that Envoy Gateway handles natively. Meanwhile, production traffic still runs through the static `cloudflare-tunnel` Helm chart (`overlays/prod/cloudflare-tunnel/values.yaml`) with 12 hardcoded routes — the operator isn't serving production yet.

The operator's Gateway API implementation is also incomplete: it uses a custom `cloudflare` GatewayClass rather than conforming to a real data-plane implementation, so HTTPRoutes don't get actual in-cluster load balancing — `cloudflared` proxies directly to backend Services.

---

## Decision

Replace the operator's in-cluster routing with Envoy Gateway and reduce the operator to Cloudflare-side lifecycle only.

### Layer 1 — Envoy Gateway (upstream, not owned)

Envoy Gateway provides a conformant Gateway API implementation:

- `Gateway` resources per trust boundary (public, private)
- `HTTPRoute` resources for per-app routing with real in-cluster load balancing
- TLS termination, header/path matching, traffic splitting — all upstream

This replaces the operator's `GatewayClass`, `Gateway`, `HTTPRoute`, and `Service` controllers entirely.

### Layer 2 — Cloudflare Operator (reduced scope)

Three CRDs remain:

| CRD                      | Responsibility                                                                                    |
| ------------------------ | ------------------------------------------------------------------------------------------------- |
| `CloudflareTunnel`       | Manages tunnel API lifecycle, deploys `cloudflared` pointing at an Envoy Gateway Service endpoint |
| `CloudflareDNSRecord`    | Manages Cloudflare DNS CNAME records (currently embedded in HTTPRoute controller)                 |
| `CloudflareAccessPolicy` | Manages Cloudflare Zero Trust access applications (unchanged)                                     |

The tunnel-to-gateway mapping is one tunnel per Gateway — `cloudflared` points at the Envoy Gateway Service instead of resolving backends directly.

### What Gets Deleted

| Current                                                  | Replacement                                                                                                                                                                                                             |
| -------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GatewayClass` controller                                | Envoy Gateway's GatewayClass                                                                                                                                                                                            |
| `Gateway` controller (tunnel + deployment orchestration) | `CloudflareTunnel` CRD refs an Envoy Gateway; deployment orchestration (`ensureCloudflaredDeployment`, HPA, PDB, `DefaultCloudflaredReplicas`) migrates from `gateway_controller.go` into `CloudflareTunnel` controller |
| `HTTPRoute` controller (DNS + published routes)          | Envoy Gateway handles routing; `CloudflareDNSRecord` CRD handles DNS                                                                                                                                                    |
| `Service` controller (annotation convenience)            | Removed — teams create HTTPRoute + CloudflareDNSRecord directly                                                                                                                                                         |
| Published routes API calls                               | Removed — `cloudflared` uses `--url` pointing at gateway, no per-route config                                                                                                                                           |

### What Changes in `CloudflareTunnel`

```diff
 CloudflareTunnelSpec:
   name:         string
   accountId:    string
-  configSource: string        # removed — no more Cloudflare-side route config
-  ingress:      []TunnelIngress  # removed — Envoy Gateway handles routing
+  gatewayRef:                 # ref to the Envoy Gateway resource
+    name:       string
+    namespace:  string
+  replicas:     int           # cloudflared deployment sizing (currently hardcoded at 2)
```

### New CRD: `CloudflareDNSRecord`

```yaml
CloudflareDNSRecord:
  spec:
    tunnelRef:
      name: string # ref to CloudflareTunnel
    hostname: string # e.g. app.jomcgi.dev
    proxied: bool # Cloudflare proxy enabled
```

Replaces the DNS record management currently embedded in the HTTPRoute controller (lines that call `CreateTunnelDNSRecord` and store record IDs in annotations).

---

## Migration Path

### Phase 1 — Validate with Static Config

No operator changes. Deploy Envoy Gateway alongside the existing `cloudflare-tunnel` chart:

1. Install Envoy Gateway, create a `Gateway` resource with HTTPS listener
2. Create `HTTPRoute` resources for 2-3 services (e.g. `argocd.jomcgi.dev`, `signoz.jomcgi.dev`)
3. Point the existing `cloudflared` at the Envoy Gateway Service instead of direct backends
4. Remove the migrated routes from `overlays/prod/cloudflare-tunnel/values.yaml`
5. Verify: traffic still flows, latency is acceptable, Linkerd mesh compatibility works

**Success criteria:** routing changes require only `HTTPRoute` edits, no tunnel config changes.

### Phase 2 — Build Reduced-Scope CRDs

Modify existing `CloudflareTunnel` CRD to add `gatewayRef`, remove `ingress`. Build `CloudflareDNSRecord` CRD. Delete the `GatewayClass`, `Gateway`, `HTTPRoute`, and `Service` controllers.

### Phase 3 — Full Migration

Migrate all 12 static routes from the `cloudflare-tunnel` chart to `HTTPRoute` + `CloudflareDNSRecord` resources. Deprecate the static chart.

---

## Consequences

- **Short-term:** Envoy Gateway adds a new cluster dependency (~2 pods). Existing static tunnel chart continues working during migration.
- **Medium-term:** Operator codebase shrinks from 6 controllers to 3. Routing becomes portable Gateway API resources.
- **Long-term:** In-cluster routing is upstream-maintained. Operator owns only Cloudflare API integration.

### Open Questions

1. **Envoy Gateway + Linkerd** — the cluster runs Linkerd service mesh. Need to verify Envoy Gateway pods get meshed correctly and don't conflict with Linkerd's proxy injection.
2. **Resource overhead** — Envoy Gateway runs its own Envoy fleet. On a 5-node homelab cluster, need to size this appropriately (single replica with HPA may be sufficient).
3. **Public/private split** — the ADR assumes two Gateways for trust boundaries. Current setup is single tunnel for everything. Evaluate whether the split is worth the complexity for a homelab.
