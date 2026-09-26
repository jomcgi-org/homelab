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

The GKE hub has hosted every application since the 2026-08-31 cutover, with two
node pools: an on-demand `core-e2` pool for everything stateful or platform-shaped and a
Spot `ember-bricks` pool with nested virtualization for the Firecracker
bricks and the embeddings pod. The pools, the GCS buckets, and the IAM
bindings are managed with `gcloud`, not from git; ADR platform/016 is the
record of the shape. The last recorded home state was a k3s cluster pruned of
application workloads and a GPU inference host reached over the tailnet. The
hub is the sole management plane: the plan to return shipped nodes as UK home
capacity was dropped on 2026-09-18 ([#4964](https://github.com/jomcgi-org/homelab/issues/4964)
closed, not planned). The closed
[#5485](https://github.com/jomcgi-org/homelab/issues/5485) and
[#5461](https://github.com/jomcgi-org/homelab/issues/5461) decisions authorize
no teardown, migration or hardware disposal. The cluster roots are
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
validates configured authentik standing issuers and hands opted-in handlers a
`Principal`. The complete current decision, exact claim checks, surface-specific
missing and invalid token behavior, owner authorization, and result-access gaps
are recorded in `projects/monolith/ARCHITECTURE.md` section 7 (#4940, #4941).
In particular, anonymous catalogue discovery on the shared MCP mount does not
authorize an anonymous tool call, and the agent MCP mount rejects anonymous
requests. `Authority.DELEGATED` and actor/scope fields are representable but no
delegation is minted or verified.

**Cloudflare Access guards the private tier.** The Access application and
its wildcard policy on `private.jomcgi.dev/*` live in the Cloudflare
dashboard; the repo carries only the verification half,
`projects/platform/cf-ingress-library/templates/_security-policy.tpl`, which
renders an Envoy `SecurityPolicy` that validates `Cf-Access-Jwt-Assertion`
and projects the `email` claim to `X-Auth-Email`. Live consumers on the hub:
`monolith-private-cf-access` and `kargo-private-cf-access`
(`kubectl get securitypolicies -A`). ArgoCD has no UI route on the hub
(`cfIngress.enabled: false` in `projects/platform/argocd/values-gke.yaml`);
the UI is reached through the Kubernetes API over the tailnet, and only the
push webhook route below is exposed here.

Documented holes in that gate, each deliberate:

- `/webhooks/github/factory` is a separate route with no `SecurityPolicy`,
  reachable through an IP-allowlist bypass in the Access policy and
  authenticated by HMAC in the handler
  (`projects/monolith/chart/templates/httproute-private.yaml`).
- `/webhooks/github/argocd` is the ArgoCD push-sync webhook route, enabled on
  the hub by `cfIngress.githubWebhook.enabled: true` in
  `projects/platform/argocd/values-gke.yaml`. It is a separate route with no
  `SecurityPolicy`, reachable through the same IP-allowlist bypass, so GitHub
  push events reach `argocd-server` instead of falling through to the
  monolith's private route. ArgoCD treats it as an unsigned refresh trigger.
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
therefore the same principal at that service until SPIFFE phase 4. The agent
application checks only that a principal is non-anonymous, not that it belongs
to `kg-agents`, so accepting the human issuer is a broader source boundary than
the guest-only topology implies. Network reachability supplies no application
authorization.

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

Restore-capability MAC key decoupling is phase 2a (#5756). Its rollout order is:
ship the default-off shared Secret wiring and a noded verifier that accepts the
dedicated key before the legacy bearer, enable the dedicated key on the control
plane and every noded pod in one staged values change, then verify restores
before rotating or retiring the transport bearer. The legacy verifier is a
one-release migration window only. Remove it in the first chart release after
the phase 2c checklist in #5706 has enabled and verified the dedicated key
fleet-wide. The repository defaults and current production values do not
enable the dedicated key, so this documents sequencing and does not claim a
rollout.

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

**GKE Dataplane V2 is the CNI, and there is no enforced private-monolith
egress policy on the hub.**
The managed dataplane exposes no `CiliumNetworkPolicy` or
`CiliumClusterwideNetworkPolicy` CRD (`kubectl api-resources | grep cilium`)
and reports `Encryption: Disabled`, so nothing on the wire is encrypted by
the cluster and only Kubernetes `NetworkPolicy` can be expressed. Every
Cilium policy template in this repo is gated off in the hub overlays:
`ciliumPolicy.ingress` and `ciliumPolicy.egress` in
`projects/monolith-public/deploy/values-gke.yaml`, `ciliumPolicy.ingress`,
`ciliumPolicy.egress`, and `tokenReplayDeny` in
`projects/monolith/deploy/values-gke.yaml`,
`noded.networkPolicy` and `tokenBroker.networkPolicy` in
`projects/embervm/deploy/values-gke.yaml`, `ciliumPolicy` in
`projects/monolith-agents/deploy/values-gke.yaml`.

The chart carries a native Kubernetes `NetworkPolicy` egress arm for the
private app endpoint, but the GKE overlay leaves it disabled. Kubernetes
NetworkPolicy has no additive audit mode, so the policy remains staged until
GKE network-policy logs cover a representative window, including periodic
leader jobs and the secret-backed ICAL feed. Its API and DNS address lists are
also empty until an enabling change validates them against the live hub.

When enabled, required internal flows use namespace plus pod selectors, exact
ports, and a paired resolver address. Standard NetworkPolicy cannot match
FQDNs, so its public TCP 443 grant excludes private, loopback, link-local,
shared-address, and multicast ranges but cannot deny an arbitrary public HTTPS
host. That is an accepted GKE residual, not an exact replacement for Cilium's
FQDN rules. Metadata-server access would remain denied.

The Cilium arm stays available, default-off, for clusters that expose its CRD.
Its enforce mode uses exact external FQDNs. Its CoreDNS rule necessarily allows
`matchPattern: "*"` so Cilium can learn addresses for those FQDNs, leaving a DNS
channel as an accepted residual. Both arms intentionally scope default-deny to
the private app endpoint. Searxng, WhatsApp, CNPG, Atlas migration jobs, and
the separate `monolith-workflows` batch namespace retain their existing egress
behavior because they are distinct workload and credential boundaries. They
need their own destination-specific policies instead of inheriting a union of
the app's privileges.

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

**One agent egresses to a third party.** The Polylane agent
(`projects/platform/polylane/`, hub only) holds a cluster-wide read: `get` and
`list`, no `watch` and no wildcards, over 24 resource types including
`pods/log`, `configmaps`, `serviceaccounts` and all four RBAC kinds. No
credential-bearing type appears in any rule, and its single write is
`get`/`update`/`patch` pinned by `resourceNames` to the one Secret it persists
its registration into, so it cannot mint a Secret. It reaches the vendor over
its own outbound `cloudflared`, so there is no inbound port and no exported
kubeconfig; revocation is deleting the Application or the API key. Pod log
content leaves the cluster on this path. Its `NetworkPolicy` is on and is
ingress-only by upstream's own rule: egress is `- {}`, which is what the tunnel
and the kube API need.

**The guest MCP tier has a separate repository-scoped observation role.** The
`monolith-agents` chart defines its own Roles for core workload status, events,
bounded logs, workload controllers, and pod metrics in an enumerated namespace
list. Argo CD Application reads are a Role in `argocd`; Kargo Freight reads are
Roles in `kargo-monolith` and `kargo-embervm`. One independent ClusterRole has
only nodes, namespaces, and node metrics. All rules are get/list except the
get-only `pods/log`; there is no watch, mutation, Secret, exec, attach,
port-forward, proxy, wildcard group, wildcard resource, or reuse of the private
monolith ClusterRole.

The pruned `agent_kubernetes` package is the matching application boundary. It
constructs requests only from the same resource and namespace table, bounds
pages, log time and size, and API timeouts, and returns explicit freshness and
coverage. Every call requires the standing, undelegated `kg-agent-sa` workload
principal in `kg-agents`, even though the tier's four knowledge tools retain
their existing non-anonymous gate. The package cannot import `cluster`, and the
private import guard continues to enforce that exclusion.

This is source and rendered-manifest state, not deployment evidence. The hub
uses a separately published and pinned chart. No permission is live merely
because this repository contains it, and this change makes no rollout claim.

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

(see: `projects/platform/cloudflare-gateway/`, `projects/platform-gke/tailscale/`, `projects/firecracker/substrate/egress-proxy/`, `projects/platform/polylane/values.yaml`, `projects/*/deploy/values-gke.yaml`)

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

No automated check on secret hygiene runs in CI beyond Semgrep Managed
Scans' registry rules: the repo's own rules for a hardcoded secret or an
empty item path were removed (#4777), so review has to catch them.

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
RuntimeDefault`). The private monolith applies that context to its backend,
progress-ingest, frontend and optional WhatsApp gateway containers. Each gets
a separate writable `/tmp` `emptyDir`, owned through pod `fsGroup: 65532`;
the remainder of each root filesystem is read-only. No admission policy
enforces any of it. Kyverno runs two `ClusterPolicy` objects, both `Audit`
(`kubectl get clusterpolicies`):
`require-resource-requests`, scoped to the `monolith` and `monolith-public`
namespaces, and `clone-monolith-workflows-secrets`, which copies Secrets
into the job namespace. The OTel injection policy is disabled. Nothing is
rejected at admission, and no check flags a new exception: the repo's
`no-privileged` and `no-host-network` Semgrep rules were removed (#4777).

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
| Semgrep registry rules | Semgrep Managed Scans, configured on the Semgrep side | live; not in this repo |
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
| `public_reader` grant, migration size, em-dashes | PreToolUse hooks in `.claude/settings.json`, `bazel/tools/hooks/` | Claude-side only; a hand edit or any other agent bypasses them |
| Chart `version:` and `targetRevision:` moved on a branch | `bazel/tools/ci/chart_version_guard.py` in PR checks | every author; lowering a pin (the revert lever) is allowed |
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
- A private monolith endpoint that reads cluster resources needs matching
  permissions in `projects/monolith/chart/templates/rbac.yaml`. An agents-tier
  observation instead belongs in its independent namespaced Roles and narrow
  cluster exception, never the private ClusterRole.
- Anything served on the public apex: the public-tier checklist.
- A new guest egress destination: an entry in `egress.internal.allowlist`,
  which is global to every egress-enabled workload.
- Identity comes from a verified claim or token, never from a header a
  caller can set; hand-pinned image digests go stale after the next
  rebuild.

(see: `bazel/tools/hooks/`, `projects/monolith/BUILD`, `.claude/settings.json`)

**Why.** Semgrep was moved into Bazel so a rule would run as a cached test
with hermetic, digest-pinned inputs (ADR security/001). Its fail-open wrapper
let the engine stop scanning without a red, so for months the repo's 93
custom rules enforced nothing, and the semgrep guest that also carried them
went unused. Both were removed (#4777) in favour of Semgrep Managed Scans,
which is why the list above states enforcement rather than intent.

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
| Review-time static checks | Semgrep Managed Scans (registry rules) |
| Findings register | `docs/THREAT-MODEL.md`, the `security-finding` label, `projects/*/stpa/security.json` |

## Direction

Decided and not yet built, each with the issue that tracks it. A row leaves
this table when the work ships or the issue closes without it.

| Direction | Decided in | Tracks | State |
| --- | --- | --- | --- |
| Public-tier egress scopes to its four documented destinations, enforced on the hub | Network | #5276 | not started |
| Per-workload EmberVM egress allowlists replace the single allowlist shared across every workload | Network | #5320 | not started |
| Optional delegated authority, a GitHub broker, or session delivery is selected only after its caller and permission or delivery gate fires | Identity | #4940, #4943, #4944, #4945, #4946 | gated; no proposal selected or shipped |

## Decision history

The ADR files were removed on 2026-09-06 (#4667); `git log -- docs/decisions/`
has the full text.

Rationale only; these records describe decisions, not current state. This
document carries what shipped.

| ADR | Decision | Status today | Disposition |
|-----|----------|--------------|-------------|
| security/001 Hermetic Semgrep via Bazel | vendor `semgrep-core` as an OCI artifact and run rules as cached Bazel tests | Superseded: the Bazel layer never scanned and was removed; CI scanning is Semgrep Managed Scans (#4777) | deleted |
| security/002 Semgrep rule generation via RL | RL-finetuned model generates rules from CVEs | Deprecated; nothing live | deleted |
| security/003 gVisor RuntimeClass | `runsc` for agent sandbox pods | Accepted, never built (#3894); the sandboxes it targeted became Firecracker guests | deleted |
| security/004 Public read-only service isolation | separate public composition, `public_reader` on a replica, default-deny egress | Accepted; composition, role, replica, imports test and private rootfs hardening shipped; private egress policy is gated pending live audit and inert on the hub (#3897, #5277); tracking #3895 | deleted |
| security/005 Public chat adversarial hardening | Turnstile sessions, reserved headroom, server-side limits, DB-confined retrieval | Implemented except the purge (#3899); inference moved off-cluster | deleted |
| security/006 Friends authorization lane | `/moving` on `friends.jomcgi.dev` behind an authentik `family` group | Accepted, shipped (#4968) | deleted |
| security/007 Aggregate threat model index | one ranked index over labelled issues, re-ranked by hand | Accepted; decisions 1, 2 and 4 live, decision 3 superseded by STPA lenses (#5294) | deleted |
