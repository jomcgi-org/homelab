# Security Model

Current state of the controls that hold the homelab together, and where
each one is enforced. Decision rationale lives in the **Why** paragraph
under each section; this page says what is live today. Open findings and
the per-surface ranking live in [THREAT-MODEL.md](THREAT-MODEL.md); open
work is tracked in GitHub Issues, never here.

Read this first for any security-sensitive change. For anything that
serves data on the public apex, read
[`docs/runbooks/public-tier-checklist.md`](runbooks/public-tier-checklist.md)
next. Platform mechanics (ingress library, delivery, storage) are in
`projects/platform/ARCHITECTURE.md`; the sandbox's own threat model is
`projects/embervm/ARCHITECTURE.md` section 10.

## Where things run

One cluster hosts every application: the GKE hub, two node pools, an
on-demand `core-e2` pool for everything stateful or platform-shaped and a
Spot `ember-bricks` pool with nested virtualization for the Firecracker
bricks and the embeddings pod. The pools, the GCS buckets, and the IAM
bindings are managed with `gcloud`, not from git; ADR platform/016 is the
record of the shape. The home k3s cluster was pruned of its workloads on
2026-08-31 and runs only the GPU inference host, which the hub reaches over
the tailnet until #5485 finishes the teardown. The cluster roots are
`projects/platform-gke/kustomization.yaml` and
`projects/gke-apps/kustomization.yaml`; every chart carries a
`values-gke.yaml` overlay, and that overlay is where most security-relevant
keys are set, so read it before the base values file.

(see: `projects/platform-gke/`, `projects/gke-apps/`, `projects/platform/ARCHITECTURE.md` section 1)

**Why.** Spot preemptions were blipping the platform singletons several
times a day, and the E2 family has no nested virtualization, so no single
pool could host both the platform tier and a brick (ADR platform/016). An
on-demand N2 core that also anchored a brick was rejected as paying to mask
two open EmberVM recovery bugs; an all-Spot layout was rejected as the least
stable option; a tiny on-demand seat for a few singletons was rejected on
placement-rule cost per dollar saved. The two-pool shape accepts that
stateful EmberVM volumes stay on Spot and recover from their GCS export.

## Trust boundaries

Six lanes, each a different audience and a different gate. The hostnames
are the live `HTTPRoute` set on the shared Gateway (`kubectl get httproutes -A`).

| Lane | Host | Who | Gate |
|------|------|-----|------|
| Public | `jomcgi.dev` | anyone | none at the edge; served by a separate binary and Postgres roles (below), rate-limited per route |
| Friends | `friends.jomcgi.dev` | authentik `family` on `/moving`, `homelab-admin` on `/preview/` | Envoy `SecurityPolicy` doing authentik OIDC plus a `groups` claim check, `defaultAction: Deny`; no Cloudflare Access application |
| Private | `private.jomcgi.dev` | Joe, plus service tokens | Cloudflare Access at the edge, then a `SecurityPolicy` validating the Access JWT on the monolith and Kargo routes |
| Machine | `mcp.jomcgi.dev` | MCP clients | no `SecurityPolicy`; Context Forge validates the authentik bearer itself |
| Identity provider | `auth.jomcgi.dev` | anyone, for the login flow | authentik's own session and MFA; the admin console is behind Cloudflare Access, configured in the dashboard |
| Tailnet | the Tailscale operator | tailnet members | the Kubernetes API through the operator's API-server proxy, and the monolith API as `monolith` on the tailnet |

Inside the cluster there is one boundary that holds by construction, the
EmberVM guest (no NIC, no ServiceAccount, vsock only), and almost no network
policy: see Network below.

(see: `projects/platform/cf-ingress-library/`, `projects/monolith/chart/templates/httproute-*.yaml`, `projects/mcp/context-forge-gateway/chart/templates/httproute-*.yaml`, `projects/platform-gke/tailscale/`)

**Why.** Per-service hostnames were rejected because each one added DNS and
optional SSO by hand; audience tiers make private exposure the default and
public exposure explicit (ADR networking/002). The friends lane exists
because the Cloudflare Access policy on the private host carries no group
concept and is shared with the operator tools, so a narrower grant there was
one binding mistake away from Kargo; a hostname that carries nothing else is
the boundary (ADR security/006). Moving the operator tools behind authentik
was deferred, not rejected, because an authentik outage must not lock the
operator out of the tools used to fix authentik. The tailnet lane adds a
second credential that reaches the API server with no Google login; the
tailnet ACL is the other half of that control, and the binding grants only
what the same person already holds.

## Identity

**authentik is the standing identity provider.** Blueprints in
`projects/platform/authentik/blueprints/` define one OAuth2 provider and
one application per lane (`dev`, `kargo`, `preview`, `moving`, `mcp`), each
bound to a group (`homelab-admin` or `family`). Renaming a group in authentik
silently breaks every lane bound to it: the `groups` claim is matched by
name. authentik runs on the hub with its own CNPG cluster and publishes
`auth.jomcgi.dev` with no `SecurityPolicy`, because the login and setup
flows share one API prefix and cannot be gated by path.

**The monolith verifies, it does not issue.** `projects/monolith/auth/`
validates RS256 bearer tokens against the authentik JWKS, requires `exp`,
`iss`, `aud` and `sub`, and hands handlers a `Principal`. A missing token
resolves to an anonymous least-privilege principal; a present but invalid
token always raises. The middleware is mounted only on `/mcp`
(`stateless_http=True`, so the principal is per request rather than pinned
to the session opener). Nothing in the monolith attenuates or delegates:
`Authority.DELEGATED` is declared in `auth/principal.py` and never
constructed (#4940, #4943, #4944).

**Cloudflare Access guards the private tier.** The Access application and
its wildcard policy on `private.jomcgi.dev/*` live in the Cloudflare
dashboard; the repo carries only the verification half,
`projects/platform/cf-ingress-library/templates/_security-policy.tpl`, which
renders an Envoy `SecurityPolicy` that validates `Cf-Access-Jwt-Assertion`
and projects the `email` claim to `X-Auth-Email`. Live consumers on the hub:
`monolith-private-cf-access` and `kargo-private-cf-access`
(`kubectl get securitypolicies -A`). ArgoCD has no route on the hub
(`cfIngress.enabled: false` in `projects/platform/argocd/values-gke.yaml`);
it is reached through the Kubernetes API over the tailnet.

Documented holes in that gate, each deliberate:

- `/webhooks/github/semgrep` and `/webhooks/semgrep` are a separate route
  with no `SecurityPolicy`, reachable through an IP-allowlist bypass in the
  Access policy and authenticated by HMAC in the handler
  (`projects/monolith/chart/templates/httproute-private.yaml`).
- `/img/` on `private.jomcgi.dev` is the public tier's imgproxy route and
  carries no `SecurityPolicy`; the edge Access policy is the only gate.
- `friends.jomcgi.dev` has no Access application. The authentik
  `SecurityPolicy` is the only control, and `/.well-known/` and `/servers/`
  on that host are unauthenticated by design so OAuth discovery works
  (`projects/mcp/context-forge-gateway/chart/templates/httproute-scoped.yaml`).

**Identity headers are stripped at the listener.** Both lanes project a
verified claim into `X-Auth-Email`, and Envoy's JWT filter appends rather
than replaces, so the gateway-wide `ClientTrafficPolicy` removes any inbound
`X-Auth-Email` before either auth filter runs
(`projects/platform/cloudflare-gateway/templates/client-traffic-policy.yaml`).
Handlers on the friends lane read the header for "whose view" only;
authorization is the `groups` claim inside the verified token.

**MCP identity has two entry points.** External callers reach Context Forge
on `mcp.jomcgi.dev` (`projects/mcp/ARCHITECTURE.md`): it validates the
authentik token, filters tools per caller, and forwards the token to the
monolith, which verifies it again. Per-caller result scoping is not live
(#4569). EmberVM agent guests take a second path: the guest's MCP URL is
delivered as a kernel boot argument, the egress sidecar admits the
`monolith-agents` service by name and address, and the token broker mints
an authentik token for one shared service account (provider `mcp-agents`,
`tokenBroker.grants` in `projects/embervm/deploy/values-gke.yaml`), which
`monolith-agents` validates alongside the human `mcp-friends` issuer
(`projects/monolith-agents/deploy/values.yaml`, `auth:`). Every guest is
therefore the same principal at that service until SPIFFE phase 4.

**SPIFFE identity exists and nothing presents it yet.** SPIRE runs on the
hub (`projects/platform/spire`, Application `projects/platform-gke/spire`):
trust domain `embervm.jomcgi.dev`, server on the core pool with its
datastore as a database on `monolith-pg`, an agent on every node attesting
with `k8s_psat`, and the CSI driver. Three `ClusterSPIFFEID` objects issue
an X.509-SVID of the form `spiffe://embervm.jomcgi.dev/ns/<ns>/sa/<sa>` to
every pod in the `embervm`, `monolith` and `monolith-agents` namespaces
(`kubectl get clusterspiffeids`). The token broker chart can mount the
Workload API and serve `/token` over SPIFFE mTLS on a second port, gated on
`tokenBroker.spiffe.enabled`, which is false with no hub override (#5755);
nothing else mounts it, the control-plane-to-noded hop still carries a
static bearer token, and guests hold no identity. Phase 2 is #5755 to
#5759; the phase plan is #5706.

**Discord features are allow-list only** (`projects/monolith/chat/acl.py`,
the `discord_feature_grant` table). **Codex OAuth is refreshed by one
process**, the token broker (`projects/embervm/tokenbroker/`), so no
`OPENAI_API_KEY` exists and no `auth.json` is fanned out.

(see: `projects/monolith/auth/`, `projects/platform/authentik/`, `projects/platform/spire/`, `projects/embervm/tokenbroker/`, `projects/monolith-agents/`)

**Why.** A Kubernetes-only identity conflated the credential holder, the
isolation principal, and permission to act, and TokenReview has no browser
or device flow, so authentik became the standing provider with local token
verification in the resource server (ADR embervm/032, ADR agents/059).
Trusted identity headers were rejected because any in-cluster caller could
forge them; a verifiable bearer keeps the monolith responsible for
validation, accepting expiry-bounded revocation and no new logins during an
identity-provider outage. For workload identity, extending the token broker
with SPIFFE-shaped JWTs was rejected because it attests nothing and
federates with nothing, and GKE managed workload certificates were rejected
because they cannot attest a Firecracker guest and do not exist on the
on-prem side, so self-hosted SPIRE was chosen with one trust domain and
per-principal guest identity (ADR embervm/041).

## Network

**Nothing is exposed directly.** The only ingress is two `cloudflared`
replicas (`projects/platform/cloudflare-gateway/values-gke.yaml`) with an
empty route list: every hostname falls through `catchAll` to the Envoy
Gateway `cloudflare-ingress`, and per-chart `HTTPRoute`s decide what a host
and path reach. There is no `Service` of type `LoadBalancer` outside
`kube-system` and no `Ingress` object; the route table is the allowlist.
Tunnel protocol is pinned to `http2`. The Tailscale operator is the second
way in: it publishes the Kubernetes API to the tailnet and impersonates the
tailnet identity as a Kubernetes user bound to `cluster-admin` for the
admins listed in `projects/platform-gke/tailscale/values.yaml`, exposes the
monolith API as a tailnet host, and runs three egress bridges to the home
inference host.

**GKE Dataplane V2 is the CNI, and there is no network policy on the hub.**
The managed dataplane exposes no `CiliumNetworkPolicy` or
`CiliumClusterwideNetworkPolicy` CRD (`kubectl api-resources | grep cilium`)
and reports `Encryption: Disabled`, so nothing on the wire is encrypted by
the cluster and only Kubernetes `NetworkPolicy` can be expressed. Every
Cilium policy template in this repo is gated off in the hub overlays:
`ciliumPolicy.ingress` and `ciliumPolicy.egress` in
`projects/monolith-public/deploy/values-gke.yaml`, `ciliumPolicy.ingress`
and `tokenReplayDeny` in `projects/monolith/deploy/values-gke.yaml`,
`noded.networkPolicy` and `tokenBroker.networkPolicy` in
`projects/embervm/deploy/values-gke.yaml`, `ciliumPolicy` in
`projects/monolith-agents/deploy/values-gke.yaml`. The one live
`NetworkPolicy` is the Context Forge redis rule from the upstream subchart
(`kubectl get networkpolicies -A`). Every other pod is unrestricted in both
directions: the public tier can dial any pod (#5276 and #5142 were closed on
the chart, not the hub), and the private monolith has no egress policy
(#5277, #3897). A chart that carries a `CiliumNetworkPolicy` is documenting
an intent the hub does not enforce.

**Guest egress is brokered.** Task and session guests have no NIC. The only
way out is the vsock egress port, which noded forwards unparsed to the
`egress-proxy` sidecar (`projects/firecracker/substrate/egress-proxy/`). The
sidecar allowlists internal destinations by both name and resolved address
(`classify.go`), so a guest cannot reach the monolith API by any spelling,
and injects real credentials only for hosts in a secret's `egressTo`
(`swap.go`): the guest never holds the token. External destinations default
to allow; the internal allowlist is global to the sidecar, not per workload
(`egress.internal.allowlist` in `projects/embervm/deploy/values-gke.yaml`:
the monolith progress port and frontend, the public frontend, searxng, the
embeddings service, and the agents MCP port; #5320). Adding an entry there
is a security decision, not tuning.

**Host-level exceptions, all named** (`kubectl get pods -A -o json`,
filtered for `privileged`, `hostNetwork`, `hostPID`, and uid 0):

| Setting | Where | Why |
|---------|-------|-----|
| `privileged: true`, `/dev/kvm`, NVMe `hostPath` | `projects/embervm/chart/templates/_noded-pod.tpl` | Firecracker needs KVM, tap and DNAT in its own netns. Accepted risk in `projects/embervm/ARCHITECTURE.md` section 10; #5702 splits the network half out |
| `privileged: true`, `hostPID` | `projects/embervm/chart/templates/scratch-prep-daemonset.yaml` | `nsenter` to format the scratch disk on the host |
| `hostNetwork`, `hostPID`, root init containers | SPIRE agent DaemonSet, `projects/platform/spire/` | node attestation and the Workload API socket; upstream hardened chart with `securityContexts: true` |
| `privileged: true` | SPIFFE CSI driver, same chart | mounts the Workload API into pods |
| `privileged: true` | Tailscale proxy StatefulSets in the `tailscale` namespace | operator-managed; `NET_ADMIN` sysctls for the tailnet device |
| `runAsUser: 0` | `projects/inference/deploy/values-gke.yaml`, embeddings pod | the llama.cpp CUDA image runs as root |
| `NET_ADMIN` init container | `projects/platform/cloudflare-gateway/templates/tunnel-deployment.yaml` | inert; gated on `tunnel.envoy.enabled: false` |

The egress sidecar in every brick pod is the model the rest should follow:
uid 65532, `readOnlyRootFilesystem`, all capabilities dropped, holding the
credentials noded never sees.

(see: `projects/platform/cloudflare-gateway/`, `projects/platform-gke/tailscale/`, `projects/firecracker/substrate/egress-proxy/`, `projects/*/deploy/values-gke.yaml`)

**Why.** The former tunnel operator reimplemented routing while production
ran a static tunnel configuration, so Envoy Gateway took over in-cluster
routing and cloudflared narrowed to edge lifecycle (ADR networking/001).
Cilium was chosen on the home cluster because the sidecar mesh blocked
ordinary network policy and added a hop to every pod (ADR platform/012); the
move to the GKE hub kept a Cilium-based dataplane but traded its policy CRDs
and WireGuard for a managed one, an accepted cost recorded nowhere as a
decision, which is why the inert templates above are still in the charts.
Guest egress is a proxy rather than a NIC because host-keyed injection at
the sidecar bounds which host a credential reaches, and a guest that never
holds the token cannot leak it into a snapshot (ADR embervm/033).

## Secrets

Every external credential enters the cluster as an `OnePasswordItem` CR
synced by the 1Password Connect operator (41 items live on the hub). The
operator is one of two components installed by hand: Helm release `connect`
in the `default` namespace, with no chart or values in this repo, and the
ArgoCD bootstrap release, which ArgoCD then adopts through the `argocd`
Application (`helm list -A`). Never hardcode a secret; values files reference
1Password item paths or a `secretRef`.

The one class of secret that cannot be declarative is the
`kubernetes.io/basic-auth` secret CNPG needs for a managed role, because the
operator emits only `Opaque` secrets. Each is created once from the same
1Password item the consumer reads, and each has a note in
`projects/monolith/deploy/` (`public-reader-secret.md`,
`public-writer-secret.md`, `spire-db-secret.md`, `embervm-oplog-secret.md`,
`agents-writer-secret.md`, `cnpg-gcs-backup-secret.md`).

Cloud credentials are static keys held in 1Password: a GCS service-account
key for the CNPG backups of `monolith-pg` and `authentik-pg`, a GCS key for
the EmberVM base store (`noded.store` in
`projects/embervm/deploy/values-gke.yaml`), and Cloudflare R2 keys for the
monolith, public and agents tiers' object storage. ADR embervm/041 phase 5
would replace the EmberVM key with a federated JWT-SVID; nothing has
started.

No automated check on secret hygiene runs: the Semgrep rules that would
catch a hardcoded secret or an empty item path are inert (#4777), so review
catches what the rules would.

(see: `projects/*/chart/templates/onepassworditem*.yaml`, `projects/monolith/deploy/*.md`)

**Why.** One external secret store with a CR per item keeps every
credential out of git and every consumer declarative; the hand-created
basic-auth secrets are the operator's limitation, not a choice, and each
one is documented at the point where it would otherwise be rediscovered by
a 503.

## Sandboxing

**Untrusted code runs in Firecracker microVMs, never bare containers.**
`run_code` in the monolith (`projects/monolith/sandbox/client.py`) POSTs to
EmberVM; the guest is one-shot, has no NIC and no mounted ServiceAccount,
and is torn down after the call. Agent sessions (Claude, Codex, pi) are the
same guest class with the brokered egress above. Bricks run on the Spot
pool with nested virtualization; `projects/embervm/ARCHITECTURE.md` section
10 is the threat table.

**The jailer is built and off.** noded launches every VM through the
Firecracker jailer when `noded.jailer.enabled` is true, and the chart
default is false with no hub override ("land dark, then arm with a
values-only change", `projects/embervm/chart/values.yaml`). Today noded
execs the VMM directly, as root, inside the privileged brick pod: a VMM
escape lands in a process holding `/dev/kvm`, the store credential and the
noded bearer token (#5255). Arming it is a values flip after live
verification on one brick.

**gVisor is not in use.** The hub carries a `gvisor` RuntimeClass because
GKE registers one, but it selects nodes labelled `sandbox.gke.io/runtime`
and no pool runs GKE Sandbox (`kubectl get runtimeclass`, node labels). ADR
security/003 targeted container sandboxes that were replaced by Firecracker
guests; #3894 stays open as its record.

**Container security context is a convention, not a control.** The apko
images build as uid 65532 (`bazel/tools/oci/go_image.bzl`) and most charts
set the full hardened context (`readOnlyRootFilesystem`, `runAsNonRoot`,
`allowPrivilegeEscalation: false`, `drop: [ALL]`, `seccompProfile:
RuntimeDefault`). No admission policy enforces any of it. Kyverno runs two
`ClusterPolicy` objects, both `Audit` (`kubectl get clusterpolicies`):
`require-resource-requests`, scoped to the `monolith` and `monolith-public`
namespaces, and `clone-monolith-workflows-secrets`, which copies Secrets
into the job namespace. The OTel injection policy is disabled. Nothing is
rejected at admission, and the Semgrep rules `no-privileged` and
`no-host-network` that would flag a new exception are inert (#4777).

(see: `projects/embervm/`, `projects/monolith/sandbox/`, `projects/platform/kyverno/`)

**Why.** Per-invocation pods and etcd churn were the ceiling for short
tasks, so a Firecracker data plane behind a BEAM control plane was chosen
over container sandboxes (ADR embervm/001); gVisor solved a container
isolation problem the platform no longer has (ADR security/003). The jailer
was adopted because overcommit blast radius needed a per-VM cgroup rung
below the brick and the node, and because the escape landing zone without
it is the compromised-brick adversary the rest of the model works hardest
to contain (ADR embervm/039). A ship-dark default accepts one more values
flip in exchange for verifying the cgroup path on a live brick first.

## Public tier

`jomcgi.dev` is served by `monolith-public`, a separate chart and a separate
Python entrypoint (`projects/monolith/app/main_public.py`) whose import
closure is pruned in `projects/monolith/BUILD` and asserted by
`app/main_public_imports_test.py`. The rules that keep it fail-closed:

- **Reads go through `public_reader` on the replica.** `monolith-pg` runs
  two instances on the hub; the public service reads `monolith-pg-ro` as a
  `NOLOGIN` role created by CNPG `managed.roles`, and every public table
  needs an explicit grant (`projects/monolith/chart/migrations/*_public_reader_grant.sql`).
  A PreToolUse hook (`bazel/tools/hooks/check-public-reader-grant.sh`)
  blocks a new `CREATE TABLE` in a public schema without one; it fires only
  for edits made through Claude Code.
- **One write path.** Public chat writes as `public_writer` on the primary,
  scoped to DML on `chat_public` (`projects/monolith/chat_public_grants_test.py`).
  Sessions are Turnstile-bound with per-session token budgets and a global
  concurrency slot of one (`projects/monolith/chat_public/limits.py`). On
  the hub the chat model is an external API (`CHAT_PUBLIC_INFERENCE_URL` in
  `projects/monolith-public/deploy/values-gke.yaml`), so anonymous prompts
  leave the cluster and the slot bounds spend rather than a GPU. The
  scheduled purge ADR security/005 requires is not built (#3899).
- **No `/api` on the public origin.** The public `HTTPRoute`s
  (`projects/monolith-public/chart/templates/httproute-public.yaml`) carry
  no `/api` rule, asserted by `public_httproute_chat_guard_test`; pages
  fetch through same-origin `+server.js` proxies. The paths that reach a
  backend from the internet are `/functions/` (the FaaS router, backend
  port), two `/ember/postgres/api/` reads, `/img/` (imgproxy, backed by
  R2), and `/health/otel-collector`. Each route carries an Envoy
  `BackendTrafficPolicy` rate limit.
- **Row filtering is the query's job.** A grant admits the table; the query
  must filter (`is_global = true` or the public view).
- **Published docs pass a content gate.** `projects/monolith/knowledge/tools/public_content.py`
  fails the docs and posts generators on in-cluster hostnames, private
  addresses, node and brick names, bucket URIs, 1Password references and
  secret-shaped assignments. It is a fixed marker list: an identifier
  matching no marker publishes verbatim.

The chart's destination-scoped egress policy and its guard test
(`public_cilium_scoped_egress_guard_test.py`) describe a control the hub
cannot apply (Network above). Kargo promotes `monolith-public` on the hub,
so the live `targetRevision` on the Application, not the git pin, says what
is deployed.

(see: `projects/monolith-public/`, `projects/monolith/app/main_public.py`, `projects/monolith/chat_public/`, `docs/runbooks/public-tier-checklist.md`)

**Why.** The anonymous SSR surface once shared a pod, backend, database role
and secret set with the private application, so a public-process compromise
crossed every boundary at once (ADR security/004). A feature-flagged single
image was rejected because the private code still ships and the isolation
becomes a runtime setting that can be wrong; the replica alone was rejected
as a confidentiality boundary because a standby replicates every row.
Separate compositions plus an engine-enforced role make private exposure
the default. Public chat added Turnstile sessions, server-side limits and
DB-confined retrieval because an anonymous generative endpoint is
adversarial by default and must never degrade the private surface (ADR
security/005).

## Findings and threat model

[`docs/THREAT-MODEL.md`](THREAT-MODEL.md) is the one ranked index of open
security findings across every hosted surface: assets, trust boundaries,
and the top of the list with a sentence of context each. The live list is
the `security-finding` GitHub label; closing the issue is how a finding
leaves the list, and the index is re-ranked by hand when the label query
changes shape. Depth lives in each project's STPA security lens
(`projects/<domain>/stpa/security.json`, rendered into `STPA.md`), where
every security row carries a `status` (`enforced-prod`, `enforced-dev`,
`shipped-off`, `designed`, `none`) and an `issue`. The procedure for adding,
closing and reviewing a finding is
[`docs/runbooks/threat-model-maintenance.md`](runbooks/threat-model-maintenance.md).

(see: `docs/THREAT-MODEL.md`, `docs/runbooks/threat-model-maintenance.md`, `projects/*/stpa/security.json`)

**Why.** The first per-domain threat model went stale within two days of
merging, and residuals for every other surface were scattered across
unlabelled issues with no page to read (ADR security/007). A hand-maintained
rollup that restated each domain's findings was rejected because it doubles
the staleness surface; a generated rollup was deferred until the
hand-maintained index has demonstrably gone stale twice. The public-repo
cost of ranking open weaknesses in one place was accepted because the
issues are already public, the index carries blast radius rather than
exploit steps, and a private list would contradict issues being the source
of truth. Per-domain prose models were superseded by the STPA security lens
(#5294), which keeps findings as extracted rows rather than narrative.

## Static checks and review gates

What a change is checked against, and by what. A row marked review-only is
a convention this document asks for and nothing enforces.

| Check | Mechanism | State |
|-------|-----------|-------|
| Custom Semgrep rules (93 under `bazel/semgrep/rules/`) | `semgrep_test` targets in `pr-checks` | inert: the vendored engine cannot load its shared libraries on the Linux runner and the wrapper exits 0 (#4777); the packaging fix merged (#5746) but the rebuilt images are blocked on a GHCR credential, and the fail-closed wrapper is parked until they exist |
| Chart admissibility (kubeconform) | #5337 | not merged |
| Pod hardening and privilege | Kyverno | two policies, both `Audit`, neither about security context |
| Resource requests and a memory limit | Kyverno `require-resource-requests` | `Audit`, `monolith` and `monolith-public` only |
| Public binary import closure | `main_public_imports_test` | live in CI |
| Public route shape | `public_httproute_chat_guard_test` | live in CI |
| Public chat grants | `chat_public_grants_test` | live in CI |
| Public library image digests | `public_library_image_digest_guard_test` | live in CI |
| Published doc content | `public_content.py` in the Format stage | live in CI, marker list only |
| Retired ADR path guard | `bazel/tools/format/doc_links/check_doc_links.py` | live in CI |
| STPA security rows carry `status` and `issue` | the `stpa` skill | skill-side, at authoring time |
| `public_reader` grant, migration size, chart and `targetRevision` sync, em-dashes | PreToolUse hooks in `.claude/settings.json`, `bazel/tools/hooks/` | agent-side only; a hand edit or a Codex worker bypasses them |
| `kubectl` read-only | convention; the `no-kubectl-mutate` rule is inert | review-only |

**Adding or changing a service, what to verify by hand**, because the
column above says nothing will do it for you:

- Non-root (uid 65532), `readOnlyRootFilesystem`, `allowPrivilegeEscalation:
  false`, `drop: [ALL]`, `seccompProfile: RuntimeDefault` in the chart. Any
  exception goes in the host-level table above with its reason.
- CPU and memory requests and a memory limit.
- What the pod reaches and what reaches it. On the hub only a Kubernetes
  `NetworkPolicy` is enforceable; a `CiliumNetworkPolicy` template documents
  intent and nothing more.
- Secrets only as `OnePasswordItem`; a CNPG role password is the one
  hand step and gets a note in `deploy/`.
- Ingress as an `HTTPRoute` on the shared Gateway with an `ingress-tier`
  label and a `SecurityPolicy` for anything not public; never a
  `LoadBalancer` or an `Ingress`.
- A monolith endpoint that reads cluster resources needs matching
  `ClusterRole` verbs (`projects/monolith/chart/templates/rbac.yaml`); a
  missing verb fails in prod as a generic 5xx.
- Anything served on the public apex: the public-tier checklist.
- A new guest egress destination: an entry in `egress.internal.allowlist`,
  which is global to every egress-enabled workload.
- Identity comes from a verified claim or token, never from a header a
  caller can set; hand-pinned image digests go stale after the next
  rebuild.

(see: `bazel/semgrep/rules/`, `bazel/tools/hooks/`, `projects/monolith/BUILD`, `.claude/settings.json`)

**Why.** Semgrep was moved into Bazel so a rule runs as a cached test with
hermetic, digest-pinned engine and rule inputs, in seconds instead of the
minutes managed CI took, because agent loops need identical results from
identical inputs (ADR security/001). The RL-generated rule pipeline that was
to feed it was deprecated (ADR security/002). The same fail-open wrapper
that made the tests fast is what let the engine stop scanning without a
red, which is why the list above states enforcement rather than intent.

## Where each control is enforced

| Control | Enforced in |
|---------|-------------|
| Ingress allowlist | `projects/*/chart/templates/httproute*.yaml`, rendered onto `cloudflare-ingress` |
| Cloudflare Access JWT | `projects/platform/cf-ingress-library/templates/_security-policy.tpl` |
| authentik OIDC lanes | `projects/platform/authentik/blueprints/`, `projects/monolith/chart/templates/httproute-friends.yaml`, `projects/mcp/context-forge-gateway/chart/templates/httproute-preview.yaml` |
| Identity header strip | `projects/platform/cloudflare-gateway/templates/client-traffic-policy.yaml` |
| Bearer verification | `projects/monolith/auth/`, `projects/monolith-agents/` (`auth:` values) |
| MCP tool visibility | `projects/mcp/context-forge-gateway/deploy/values.yaml` (`tools.visibility`, teams) |
| Workload identity issuance | `projects/platform/spire/values.yaml` (`clusterSPIFFEIDs`) |
| Tailnet access | `projects/platform-gke/tailscale/values.yaml` (`apiServerProxyAdmins`, `egressServices`), `projects/monolith/chart/templates/service-tailnet.yaml` |
| Pod network policy | none on the hub beyond the Context Forge redis rule; templates in `projects/{monolith,monolith-public,embervm,monolith-agents}/chart/templates/*policy*.yaml` are gated off in `values-gke.yaml` |
| Guest egress allowlist and credential swap | `projects/firecracker/substrate/egress-proxy/cmd/`, catalog in `projects/embervm/deploy/values-gke.yaml` |
| Secrets | `OnePasswordItem` CRs per chart; operator is the `connect` Helm release outside git |
| Sandbox | `projects/embervm/` (Firecracker), entry `projects/monolith/sandbox/client.py`; jailer gated on `noded.jailer.enabled` |
| Public data isolation | `public_reader` and `public_writer` grants, `main_public_imports_test`, `public_httproute_chat_guard_test`, `chat_public_grants_test`, `check-public-reader-grant.sh` |
| Public rate limits | `BackendTrafficPolicy` objects in `projects/monolith-public/chart/templates/` |
| Published content | `projects/monolith/knowledge/tools/public_content.py` |
| Review-time static checks | `bazel/semgrep/rules/` (inert until #4777 closes) |
| Findings register | `docs/THREAT-MODEL.md`, the `security-finding` label, `projects/*/stpa/security.json` |

## Decision history

The ADR files were removed on 2026-09-06 (#4667); `git log -- docs/decisions/`
has the full text.

Rationale only; these records describe decisions, not current state. This
document carries what shipped.

| ADR | Decision | Status today | Disposition |
|-----|----------|--------------|-------------|
| security/001 Hermetic Semgrep via Bazel | vendor `semgrep-core` as an OCI artifact and run rules as cached Bazel tests | Accepted; every target passes without scanning (#4777, #3893) | deleted |
| security/002 Semgrep rule generation via RL | RL-finetuned model generates rules from CVEs | Deprecated; nothing live | deleted |
| security/003 gVisor RuntimeClass | `runsc` for agent sandbox pods | Accepted, never built (#3894); the sandboxes it targeted became Firecracker guests | deleted |
| security/004 Public read-only service isolation | separate public composition, `public_reader` on a replica, default-deny egress | Accepted; composition, role, replica and imports test shipped; egress policy inert on the hub (#3897, #5277); read-only rootfs open (#3898); tracking #3895 | deleted |
| security/005 Public chat adversarial hardening | Turnstile sessions, reserved headroom, server-side limits, DB-confined retrieval | Implemented except the purge (#3899); inference moved off-cluster | deleted |
| security/006 Friends authorization lane | `/moving` on `friends.jomcgi.dev` behind an authentik `family` group | Accepted, shipped (#4968) | deleted |
| security/007 Aggregate threat model index | one ranked index over labelled issues, re-ranked by hand | Accepted; decisions 1, 2 and 4 live, decision 3 superseded by STPA lenses (#5294) | deleted |
