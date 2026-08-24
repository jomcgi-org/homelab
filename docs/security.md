# Security Model

Current state of the controls that hold the homelab together, and where
each one is enforced. Decision rationale lives in `docs/decisions/`; this
page says what is live today. Open work is tracked in GitHub Issues
(`gh issue list --search security`), never here.

Read this first for any security-sensitive change. For anything that
serves data on the public apex, read
[`docs/runbooks/public-tier-checklist.md`](runbooks/public-tier-checklist.md)
next.

## Trust boundaries

Four tiers, each a different audience and a different gate:

| Tier | Host | Who | Gate |
|------|------|-----|------|
| Public | `jomcgi.dev` | anyone | none; served by a separate binary and Postgres role (below) |
| Friends | `friends.jomcgi.dev` | authentik `family` group | Envoy `SecurityPolicy` with authentik OIDC, no Cloudflare Access |
| Private | `private.jomcgi.dev` | Joe, plus service tokens | Cloudflare Access JWT validated in Envoy |
| Machine | `mcp.jomcgi.dev`, `private.jomcgi.dev/llm/v1` | MCP clients, OpenAI-compatible SDKs | authentik bearer (MCP); Access service token only (`/llm`) |

Inside the cluster, the boundaries are namespaces with Cilium policies on
the workloads that opted in, and the EmberVM guest boundary for anything
that runs untrusted code.

## Identity

**authentik is the standing identity provider.** Blueprints in
`projects/platform/authentik/blueprints/` define one OAuth2 provider and
one application per lane (`dev`, `kargo`, `preview`, `moving`, `mcp`), each
bound to a group (`homelab-admin` or `family`). Renaming a group in authentik
silently breaks every lane bound to it: the `groups` claim is matched by
name.

**The monolith verifies, it does not issue.** `projects/monolith/auth/`
validates RS256 bearer tokens against the authentik JWKS (`verifier.py`),
requires `exp`, `iss`, `aud` and `sub`, and hands handlers a `Principal`
(`principal.py`). A missing token resolves to an anonymous least-privilege
principal; a present but invalid token always raises. The middleware is
mounted only on `/mcp` (`framework/core.py`, `stateless_http=True` so the
principal is per request rather than pinned to the session opener). Nothing
in the monolith attenuates or delegates yet: `Authority.DELEGATED` is
declared and never constructed (#4940, #4943, #4944).

**Cloudflare Access guards the private tier.** The Access application and
its wildcard policy on `private.jomcgi.dev/*` live in the Cloudflare
dashboard; the repo carries only the verification half,
`projects/platform/cf-ingress-library/templates/_security-policy.tpl`, which
renders an Envoy `SecurityPolicy` that validates `Cf-Access-Jwt-Assertion`
against `jomcgi.cloudflareaccess.com` and projects the `email` claim to
`X-Auth-Email`. Live consumers: `argocd`, `inference`, `kargo`, `longhorn`,
`monolith`, `signoz` (`kubectl get securitypolicies -A`).

Documented holes in that gate, each deliberate:

- `/webhooks/github/argocd` and `/webhooks/github/semgrep` are separate
  routes with an IP-allowlist bypass (`projects/platform/argocd/templates/httproute.yaml`,
  `projects/monolith/chart/templates/httproute-private.yaml`).
- `/llm` and `/llm/v1` accept Access service tokens, which arrive without
  a JWT, so the JWT provider there is `optional: true` and Access alone
  guards the route. Anything in-cluster that can reach the Envoy gateway
  with a `private.jomcgi.dev` Host header bypasses it
  (`projects/inference/deploy/templates/httproute-private.yaml`).
- `friends.jomcgi.dev` has no Access application. The authentik
  `SecurityPolicy` is the only control, and `/.well-known/` and `/servers/`
  on that host are unauthenticated by design (`projects/mcp/context-forge-gateway/chart/templates/httproute-scoped.yaml`).

**MCP identity runs through Context Forge** (`projects/mcp/ARCHITECTURE.md`).
The gateway validates the authentik token, filters tools per caller by
tag and team, and forwards the caller's token to the monolith, which
verifies it again. Per-caller result scoping is not live (#4569). The
token-replay containment that `projects/mcp/ARCHITECTURE.md` and the
`mcp-auth.yaml` blueprint describe is a `CiliumNetworkPolicy` in the
monolith chart gated on `ciliumPolicy.tokenReplayDeny.enabled`, which is
`false` and has no deploy override: it does not render.

**Header identity exists on one lane.** `X-Auth-Email` is injected on
`friends.jomcgi.dev` and read by `projects/monolith/agent_sessions/router.py`
and `projects/monolith/moving/`. In-cluster forgery is blocked by the
monolith ingress allowlist (below); nothing off that lane authorizes on
the header.

**Discord features are allow-list only.** `projects/monolith/chat/acl.py`
and the `discord_feature_grant` table replace any owner check (ADR
agents/029, shipped).

**Codex OAuth is refreshed by one process.** The token broker
(`projects/embervm/tokenbroker/`) is the single owner of the rotating
grant; no `OPENAI_API_KEY` exists and no `auth.json` is fanned out (ADR
agents/048, shipped).

## Network

**Nothing is exposed directly.** The only ingress is four `cloudflared`
replicas (`projects/platform/cloudflare-gateway/`) with an empty route
list: every hostname falls through `catchAll` to the Envoy Gateway
`cloudflare-ingress`, and per-chart `HTTPRoute`s decide what a host and
path reach. A hostname that resolves reaches the gateway; the route table
is the allowlist. Tunnel protocol is pinned to `http2`.

**Cilium is the CNI and the mesh.** `projects/platform/cilium/values.yaml`:
WireGuard pod-to-pod encryption on, Hubble on, `kubeProxyReplacement`
on, `policyEnforcementMode: default`. There is no cluster-wide
default-deny and no `CiliumClusterwideNetworkPolicy`; a pod is
unrestricted until a chart opts it into a policy. `nodeEncryption: true`
is set but three of four agents report `NodeEncryption: OptedOut`
(#5146), so do not claim node-to-node encryption until that closes.

Policies that render in prod (`kubectl get cnp -A`):

| Policy | Scope | File |
|--------|-------|------|
| `monolith-api-ingress` | ingress default-deny on the monolith app pod; allows the gateway, `mcp`, jobs, whatsapp, and `embervm` on named ports | `projects/monolith/chart/templates/cilium-ingress-policy.yaml` |
| `monolith-public-{frontend,web}-ingress` | ingress default-deny; gateway and the shotter guest only | `projects/monolith-public/chart/templates/cilium-policy.yaml` |
| `monolith-public-{web,frontend,imgproxy}-egress` | egress default-deny off-cluster; `web` may reach Cloudflare ranges for Turnstile. In-cluster egress is `toEntities: cluster`, not the four dependencies (#5142) | same file |
| `embervm-embervm-noded` | ingress-only allowlist for the control plane, serving Envoy and SigNoz; egress unrestricted | `projects/embervm/chart/templates/noded-networkpolicy.yaml` |
| `embervm-embervm-tokenbroker` | ingress from noded and the monolith; egress to kube-dns, the granted FQDNs, and the apiserver | `projects/embervm/chart/templates/tokenbroker-networkpolicy.yaml` |

Two policies exist in charts and do not render: the monolith
`no-token-replay` egress deny (above) and the Context Forge
`NetworkPolicy` (`networkPolicy.enabled: false`, no deploy override).
The private monolith has no egress policy at all (#5143).

**Guest egress is brokered.** Task and session guests have no NIC. The
only way out is the vsock egress port, which noded forwards unparsed to
the `egress-proxy` sidecar (`projects/firecracker/substrate/egress-proxy/`).
The sidecar allowlists internal destinations by both name and resolved
address (`classify.go`), so a guest cannot reach `monolith:8000` by any
spelling, and injects real credentials only for hosts in a secret's
`egressTo` (`swap.go`): the guest never holds the token. External
destinations default to allow; the internal allowlist is global to the
sidecar, not per workload (`projects/embervm/deploy/values.yaml`,
`egress:` block). Leaving a catalog entry out means uncredentialed
pass-through; a blank key means deny.

**Host-level exceptions, all named:**

| Setting | Where | Why |
|---------|-------|-----|
| `privileged: true`, `/dev/kvm`, NVMe `hostPath` | `projects/embervm/chart/templates/_noded-pod.tpl` | Firecracker needs KVM, tap and DNAT in its own netns. Accepted risk in `projects/embervm/ARCHITECTURE.md` §10 |
| `privileged: true`, `hostPID` | `projects/embervm/chart/templates/scratch-prep-daemonset.yaml` | `nsenter` to format the scratch image on the host disk |
| `hostNetwork: true` | `projects/platform/signoz/values-prod.yaml` | the otel agent must hear the k3s control plane on `localhost:4317`; a Kyverno mutation adds `ClusterFirstWithHostNet` |
| `NET_ADMIN` init container | `projects/platform/cloudflare-gateway/templates/tunnel-deployment.yaml` | inert; gated on `tunnel.envoy.enabled: false` |

## Secrets

Every secret enters the cluster as an `OnePasswordItem` CR synced by the
1Password Connect operator (52 items live). The operator is the one
component outside ArgoCD: Helm release `connect` (chart `connect-2.0.5`)
in the `default` namespace, installed by hand, with no chart or values in
this repo. Never hardcode a secret; values files reference `op://` paths
or a `secretRef`.

One committed exception: `projects/monolith/chart/templates/cnpg-backup-s3-secret.yaml`
renders literal S3 keys because SeaweedFS runs with `enableAuth: false`
and the keys are placeholders. The header comment in that file is the
record.

The agent-side guard for the `public_reader` grant and the Semgrep rules
`no-hardcoded-secret`, `no-empty-1password-itempath` and
`no-hardcoded-image-digest` are the only automated checks on secret
hygiene, and Semgrep currently exits 0 without scanning (#4777), so review
catches what the rules would.

## Sandboxing

**Untrusted code runs in Firecracker microVMs, never bare containers.**
`run_code` in the monolith (`projects/monolith/sandbox/client.py`) POSTs
to EmberVM; the guest is one-shot, has no NIC and no mounted
ServiceAccount, and is torn down after the call. Agent sessions (Claude,
Codex, pi) are the same guest class with the brokered egress above.
`projects/embervm/ARCHITECTURE.md` §10 is the threat table.

**gVisor is not installed.** ADR security/003 is Accepted and unbuilt:
no `runsc`, no `RuntimeClass: gvisor`, no Kyverno rule requiring it
(#3894). The live `RuntimeClass` list carries `nvidia` (the inference and
GPU operator pods use it), a `kata-fc` handler nothing schedules on, and
the k3s wasm defaults. Six later ADRs cite 003 as a prerequisite; treat
those citations as stale.

**Container security context is a convention, not a control.** The
apko images build as uid 65532 (`bazel/tools/oci/go_image.bzl`) and most
charts set the full hardened context (`readOnlyRootFilesystem`,
`runAsNonRoot`, `allowPrivilegeEscalation: false`, `drop: [ALL]`,
`seccompProfile: RuntimeDefault`). No admission policy enforces any of
it. Kyverno runs five `ClusterPolicy` objects, all `Audit`: three
mutate (otel env injection, otel agent DNS) or generate (secret clones
into `monolith-workflows`), and the one validating policy,
`require-resource-requests`, is scoped to `monolith` and
`monolith-public`. Nothing is rejected at admission. The Semgrep rules
`no-privileged` and `no-host-network` are the review-time guards, with
the exceptions in the table above.

## Public tier

`jomcgi.dev` is served by `monolith-public`, a separate chart and a
separate Python entrypoint (`projects/monolith/app/main_public.py`) whose
import closure is pruned in `projects/monolith/BUILD` and asserted by
`app/main_public_imports_test.py`. The rules that keep it fail-closed:

- **Reads go through `public_reader` on the read replica.** The role is
  `NOLOGIN` in migrations and created by CNPG `managed.roles`; every
  public table needs an explicit grant
  (`projects/monolith/chart/migrations/*_public_reader_grant.sql`). A
  PreToolUse hook (`bazel/tools/hooks/check-public-reader-grant.sh`)
  blocks a new `CREATE TABLE` in a public schema without one; it fires
  only for edits made through Claude Code.
- **One write path.** Public chat writes as `public_writer` on the
  primary, scoped to DML on `chat_public`
  (`projects/monolith-public/chart/values.yaml`, `publicWriter:`;
  `projects/monolith/chat_public_grants_test.py`). Turnstile-bound
  sessions, per-session token budgets and a global concurrency slot of
  one are in `projects/monolith/chat_public/limits.py`. The scheduled
  purge ADR security/005 requires is not built (#3899).
- **No `/api` on the public origin.** The public `HTTPRoute`
  (`projects/monolith-public/chart/templates/httproute-public.yaml`) has
  no `/api` rule; pages fetch through same-origin `+server.js` proxies.
  `/api/chat/public/*` never appears on the route, asserted by
  `public_httproute_chat_guard_test`. The one path that reaches the
  public backend from the internet is `/functions/`, rate-limited.
- **Row filtering is the query's job.** A grant admits the table; the
  query must filter (`is_global = true` or the public view). The Semgrep
  rule `sveltekit-public-route-fetches-private-knowledge-api` covers the
  frontend half.

## Where each control is enforced

| Control | Enforced in |
|---------|-------------|
| Ingress allowlist | `projects/*/chart/templates/httproute*.yaml`, rendered onto `cloudflare-ingress` |
| Cloudflare Access JWT | `projects/platform/cf-ingress-library/templates/_security-policy.tpl` |
| authentik OIDC lanes | `projects/platform/authentik/blueprints/`, `projects/monolith/chart/templates/httproute-friends.yaml`, `httproute-private.yaml` (dev) |
| Bearer verification | `projects/monolith/auth/` |
| MCP tool visibility | `projects/mcp/context-forge-gateway/deploy/values.yaml` (`tools.visibility`, teams) |
| Pod network policy | `projects/{monolith,monolith-public,embervm}/chart/templates/*policy*.yaml`, on by `deploy/values.yaml` |
| Guest egress allowlist and credential swap | `projects/firecracker/substrate/egress-proxy/cmd/`, catalog in `projects/embervm/deploy/values.yaml` |
| Secrets | `OnePasswordItem` CRs per chart; operator is the `connect` Helm release outside git |
| Sandbox | `projects/embervm/` (Firecracker), entry `projects/monolith/sandbox/client.py` |
| Public data isolation | `public_reader` grants, `main_public_imports_test`, `public_httproute_chat_guard_test`, `check-public-reader-grant.sh` |
| Review-time static checks | `bazel/semgrep/rules/` (inert until #4777 closes) |
| kubectl | read-only by convention and the `no-kubectl-mutate` Semgrep rule; every change is a Git commit |

## ADR map

| ADR | Status | Today |
|-----|--------|-------|
| security/001 Hermetic Semgrep via Bazel | Accepted | rules run as Bazel tests inside `pr-checks`; engine currently skips targets (#4777, #3893) |
| security/002 Semgrep rule generation RL | Deprecated | no live gate |
| security/003 gVisor RuntimeClass | Accepted | unbuilt (#3894); later ADRs citing it as a prerequisite are stale |
| security/004 Public read-only service isolation | Accepted | entrypoint, role, replica, imports test shipped; egress is cluster-wide not Postgres-only (#5142); private default-deny and read-only rootfs open (#3897, #3898, #5143) |
| security/005 Public chat adversarial hardening | Implemented | all layers shipped except the scheduled purge (#3899) |
| security/006 Moving friends authorization lane | Accepted | shipped on `friends.jomcgi.dev/moving` (#4968) |
| agents/029 Discord feature ACL | Accepted | shipped |
| agents/048 Codex OAuth token broker | Accepted | shipped |
| agents/059 authentik federates, monolith serves MCP | Draft | verifier shipped; Context Forge still the entry point |
| agents/047 per-principal egress credentials | Draft | not started; `secretFor` is host-keyed |
| agents/055 tool-mediated GitHub access | Superseded by 059 | not started |
| agents/005, 006, 011 earlier MCP auth designs | Deprecated / Superseded | replaced by authentik direct |
| agents/027 GitHub App roles | Draft | not started |
| agents/034 per-tier guest MCP ACL | Draft | not started |
