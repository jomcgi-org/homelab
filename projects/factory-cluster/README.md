# Factory home cluster

The home Kubernetes cluster that hosts the replacement software factory
([#7817](https://github.com/jomcgi-org/homelab/issues/7817)). It is being
built before any workload moves from the GKE hub. This directory holds its node
bootstrap and its ArgoCD root; `projects/platform-factory/` holds one
Application per shared component.

Status (2026-11-13): nodes rebuilt as three k3s servers on Cilium; 1Password
Connect and ArgoCD run and sync `factory-root`. ArgoCD is published at
factory.jomcgi.dev behind Authentik (factory-auth.jomcgi.dev).

## Nodes

Recorded 2026-11-13, before the reset.

| Node | LAN address (DHCP) | CPU | Memory | Disks | Notes |
| --- | --- | --- | --- | --- | --- |
| node-1 | 192.168.4.80 (`enp1s0`) | 12 x86_64, VT-x | 16 GiB | 256 GB KIOXIA NVMe (OS + data) | Tailscale; operator workstation |
| node-2 | 192.168.4.81 (`enp2s0`) | 12 x86_64, VT-x | 16 GiB | 256 GB KIOXIA NVMe; 112 GB Hitachi HDD `sda` (NTFS, unused) | |
| node-3 | 192.168.4.76 (`enp2s0`) | 12 x86_64, VT-x | 16 GiB | 256 GB SK hynix NVMe | |

All three run Ubuntu 24.04.3 with kernel 6.8.0-138 and have `/dev/kvm`. The
retired home k3s was crash-looping without etcd quorum. Its join address was
192.168.1.161, and the LAN is now 192.168.4.0/22. `nodes/reset-node.sh` removed it in place:
k3s, `/var/lib/{rancher,longhorn,kubelet,embervm}` and the EmberVM scratch
loop mount. The OS, SSH and Tailscale stayed.
node-2's `sda` was not touched. node-1's host `cloudflared` (bastion.jomcgi.dev
SSH) is disabled; Tailscale replaces it.

The addresses are DHCP leases and embedded etcd records peer IPs, so the
router must reserve these three leases.

## Cluster

| Choice | Value | Why |
| --- | --- | --- |
| Distribution | k3s `v1.37.2+k3s1`, three servers, embedded etcd | In-place on the existing Ubuntu installs; tolerates one node loss. Agent Substrate needs Kubernetes 1.37+ |
| Certificates API | `certificates.k8s.io/v1beta1` served | Substrate's podcertificate controller uses the v1beta1 ClusterTrustBundle and PodCertificateRequest clients |
| Sandboxing | gVisor `release-20261102.0` (`runsc`), RuntimeClass `gvisor` | The same release Substrate's SandboxConfig pins. Substrate stages its own runsc inside worker pods; the RuntimeClass serves other sandboxed pods |
| Agent Substrate | kagent-dev fork `1.0.1` (commit `7ce6f43`): its Helm charts rendered into Git, images pinned by digest; gVisor class only. The version is set in `substrate/render-upstream.sh` and `bootstrap-substrate.sh` | The fork release line kagent v1.0.1 uses (it builds against fork v1.0.0); it adds the Kubernetes-Secret credential provider and the agentgateway egress gateway kagent injects credentials through. Its gVisor SandboxConfig defaults to `v7`, release 20261102.0, the release the nodes run ([Agent Substrate](#agent-substrate)) |
| Node sysctls | proxy ARP/NDP, larger neighbour table, inotify | Substrate's gVisor networking and sandbox density (`nodes/99-factory.conf`) |
| Network | Cilium 1.20.4: kube-proxy replacement, WireGuard pod encryption, Hubble | Cluster-wide default-deny and egress allowlists by domain name; Hubble shows what was dropped. Cilium lists Kubernetes 1.33 to 1.36 as tested, so 1.37 was validated here with `cilium connectivity test` before default-deny; rerunning it now needs a temporary policy exempting the `cilium-test-*` namespaces |
| Storage | local-path (k3s default) volumes; services replicate across nodes themselves | No replicated block storage: Garage and CloudNativePG each keep three copies on the three nodes' local disks ([Object storage](#object-storage), [Postgres](#postgres)) |
| Object storage | Garage v2.5.0, three nodes, replication factor 3, S3 at `garage.garage.svc:3900` | Workload S3 (CNPG WAL archives, itsaplan, evidence, Substrate snapshots) with plain S3 and three copies on three disks; Joe's decision on #7817. No versioning or Object Lock, so it is not a backup target |
| Images | embedded Spegel mirror for every registry | Each image crosses the home uplink once |
| Cilium devices | `enp+` (the wired NICs) | Auto-detection also took `tailscale0` on node-1, set every pod there to its 1280 MTU and silently dropped full-size QUIC replies from the Cloudflare edge; it also served KPR services on the tailnet |
| Tailscale (node-1) | netfilter mode off; `src_valid_mark=0` | With Tailscale managing iptables it sets `src_valid_mark=1`, and the kernel then drops every pod packet Cilium redirects to its DNS or L7 proxy, which breaks `toFQDNs` policies |
| Ingress | Cloudflare Tunnel managed by cfgate (Gateway API) | No inbound ports; the tunnel, its routes and DNS records are Kubernetes resources. cfgate replaced StringKe/cloudflare-operator, whose last release (v0.51.0) watches Gateway API `v1alpha2` UDPRoute, which current Gateway API no longer serves |
| Edge | Envoy Gateway 1.8.3 in `edge`, the tunnel's only origin | One proxy where sign-in is enforced for every published route; the hub runs the same release |
| Human sign-in | Authentik 2026.5.6: OIDC at the edge Gateway for every route, and each application's own OIDC where it has one; no Cloudflare Access | One identity layer, as #7817 specifies. A gateway-wide policy means a new route cannot forget sign-in |
| Admission policy | ValidatingAdmissionPolicy (`projects/platform-factory/edge-admission/`) | Keeps Gateways, routes, the edge label and Envoy's proxy and auth objects inside `edge`. The API server evaluates it in every namespace; Kyverno skips kube-system, kube-public, kube-node-lease and its own namespace |
| Postgres | CloudNativePG operator 1.30 (chart 0.29.0, as on the hub) | Authentik's database (one instance, no backups yet) and kagent's `kagent-pg` (three instances, one per node) |
| Backups | `kagent-pg`: continuous WAL archiving and daily base backups to Garage, 14 days kept (in-tree barman-cloud) | Survives losing a pod, a volume or a node. Garage shares the cluster's failure domain, so this is not off-site: losing the cluster loses the backups too. Off-site copies (R2 or GCS) are deferred |
| Secrets | 1Password Connect + operator, `k8s-homelab` vault; Authentik's secret key and OIDC client secrets are generated in the cluster | Repository convention. Authentik's secrets cannot outlive its database until Postgres has off-node backups, so a copy in 1Password would protect nothing yet |
| etcd snapshots | every 6 hours, 20 kept, on each node's disk | Off-node copies come later |
| Observability | The hub's otel-collector chart (`values-factory.yaml`) shipping to Honeycomb's `homelab` environment, tagged `k8s.cluster.name=factory` and `deployment.environment=factory` | One telemetry stack and one ingest key for both clusters. A DaemonSet reads each node's kubelet, `/proc`, Cilium agent (:9962) and Hubble (:9965) metrics and its warning-and-above pod logs (redacted; `authentik` and `ate-system` excluded); one Deployment watches Warning events and scrapes the API servers and Cilium operators. Every source is allowlisted and scraped every 120s to stay inside the shared event budget |

Traefik and ServiceLB are disabled.

## Bootstrap and rebuild

The scripts run from an operator machine with SSH and passwordless sudo on
the nodes. They are idempotent.

```bash
# 1. Wipe a node's previous install (destructive; confirm with its hostname).
ssh node-2 'sudo bash -s -- --yes node-2' < projects/factory-cluster/nodes/reset-node.sh

# 2. Install or upgrade k3s and gVisor on all nodes; writes ~/.kube/factory.yaml.
projects/factory-cluster/nodes/bootstrap-nodes.sh

# 3. Hand the cluster to ArgoCD (once).
OP_CREDENTIALS_FILE=~/factory-1password-credentials.json \
OP_CONNECT_TOKEN=... \
  projects/factory-cluster/bootstrap-gitops.sh

# 4. Generate Agent Substrate's CAs, JWT keys and snapshot-store key (once).
projects/factory-cluster/bootstrap-substrate.sh
```

Nodes register after step 2 but stay NotReady: there is no CNI yet. Step 3
installs Cilium first, then creates the two 1Password bootstrap Secrets,
installs Connect and ArgoCD from this repository's charts, and applies
`factory-root-application.yaml`. From then on, merged changes under
`projects/platform-factory/` deploy automatically, and ArgoCD adopts the
Cilium, Connect and ArgoCD installs.

To upgrade k3s or gVisor, change the pins in `nodes/install-node.sh` and rerun
step 2. It restarts one node at a time.

A lost cluster is rebuilt with steps 2 to 4. That recreates everything in
Git and new Substrate keys, but not Substrate's state: its PostgreSQL (actors,
templates, atespaces) and its actor snapshots have no backup yet, so they are
lost with the cluster or with the node holding their volume. The Substrate
CRDs, namespaces and snapshot volume are marked `Delete=false`, so deleting
or renaming the ArgoCD Application does not remove them.

etcd snapshots are encrypted with the cluster token on each server
(`/var/lib/rancher/k3s/server/token`); keeping a copy off the nodes is part of
the backup and restore work below.

## Operator inputs

These are the only things set up outside Git, plus the reason for each:

1. **1Password Connect server** `factory` with read access to the `k8s-homelab`
   vault. Its credentials file and an access token are the inputs to
   `bootstrap-gitops.sh`. They are credentials for the secret broker itself, so
   they cannot come from the broker.
2. **1Password item** `k8s-homelab/factory-cloudflare-api-token`, with a field
   `CLOUDFLARE_API_TOKEN` holding a Cloudflare API token that has:
   Account: Cloudflare Tunnel:Edit; Zone `jomcgi.dev`: DNS:Edit and Zone:Read.
3. **DHCP reservations** for the three node addresses above.

## Network policy

Every pod-network pod is default-deny in both directions
(`projects/platform-factory/network-policy/resources/baseline.yaml`);
host-network pods (the Cilium agent, Envoy and operator) and the hosts
themselves are outside it. The only
flow the baseline allows is DNS to CoreDNS, through Cilium's DNS proxy so that
egress rules can name domains. Each namespace then has one file allowing
exactly its own traffic, with internet egress listed by domain name. A
namespace created by an Application rather than bootstrap keeps its policy in
that Application (cfgate, cloudnative-pg, edge and authentik do), so
the policy lands with the namespace. A component with no policy can reach
nothing, so a new component needs one in the same change that deploys it.

Ordering matters inside an Application. A pod that starts before its policy
exists is dropped until the policy lands; for a Job that can mean failing
outright. Envoy Gateway's certgen Job is a Helm pre-install hook, which ArgoCD
runs before any ordinary resource, so its policy is a PreSync hook as well.
Authentik's policies carry an earlier sync wave than its secret Job.

A backend behind the edge Gateway needs an egress rule on the Envoy proxy
(`projects/platform-factory/edge/chart/templates/network-policy.yaml`) and an
ingress rule from the proxy in its own namespace.

To see what a policy drops:

```bash
KUBECONFIG=~/.kube/factory.yaml kubectl -n kube-system exec ds/cilium -c cilium-agent -- \
  hubble observe --verdict DROPPED -f
```

That shows one node; run it against each agent pod for the whole cluster.

## factory.jomcgi.dev

The only path in from the internet:

1. Cloudflare terminates TLS and sends the request down the `factory`
   CloudflareTunnel (`projects/platform-factory/factory-edge/`).
2. The tunnel's `cloudflare-tunnel` Gateway accepts routes only from the
   namespace named `edge` that also carries `factory.jomcgi.dev/edge=true`. Its
   one route, `edge/tunnel` (`projects/platform-factory/edge-tunnel/`), sends
   every published hostname to the Envoy proxy Service `edge/edge-proxy`. The
   route's `factory.jomcgi.dev/publish=true` annotation makes cfgate create
   the DNS records.
3. Envoy Gateway's `edge` Gateway (`projects/platform-factory/edge/`) signs
   every request in through Authentik with a gateway-wide OIDC
   SecurityPolicy, then routes it. Routes attach only from the namespaces
   listed in the chart's `routeNamespaces` (`edge`, `argocd`), matched by
   name.
4. The application can run its own Authentik login too; ArgoCD does.

| Hostname | Route | Sign-in |
| --- | --- | --- |
| factory.jomcgi.dev | `argocd/argocd-server` | Envoy OIDC (client `factory-edge`), then ArgoCD's OIDC (client `factory-argocd`); both admit only `authentik Admins` |
| factory-auth.jomcgi.dev | `edge/authentik` | Authentik itself, with MFA required |

Envoy's OIDC session lasts as long as the access token, ten minutes, with no
refresh. Envoy then sends the browser back to Authentik, which checks the
group binding again, so removing someone from `authentik Admins` ends their
access within ten minutes. The cookies are `SameSite=Lax`.

Authentik's own route is the one exemption from the gateway-wide policy:
a route-level SecurityPolicy replaces it there, since the gateway policy
redirects to Authentik. From the internet that route serves the flow pages,
static files, the OAuth2 endpoints under `/application/o/` and three API
prefixes the flow interface needs (`/api/v3/flows/executor/`,
`/api/v3/root/config/`, `/api/v3/core/brands/current/`). The rest of
`/api/v3/`, the admin and user interfaces (`/if/admin/`, `/if/user/`) and the
initial-setup flow answer 403. Authentik sees the client address from
Cloudflare's `CF-Connecting-IP`, which the route copies into
`X-Forwarded-For`.

The policies in `projects/platform-factory/edge-admission/` enforce the rest,
in every namespace: only `edge` may carry the edge label; the only
GatewayClasses are `edge` and `cfgate`, each with one Gateway in its own
namespace and no per-Gateway EnvoyProxy; EnvoyProxy, SecurityPolicy,
EnvoyPatchPolicy, EnvoyExtensionPolicy, ClientTrafficPolicy and Backend
objects live only in `edge`; any route with a parent in `cfgate-system` must
be an HTTPRoute in `edge` whose only backend is `edge-proxy`, with no filters;
only `edge` may route factory-auth.jomcgi.dev; and CloudflareDNS and
CloudflareTunnel objects live only in `cfgate-system`. Envoy Gateway runs each
proxy in its Gateway's namespace, so no other Gateway's proxy could run
under the edge proxy's namespace and network identity.

If `edge/edge-oidc-client` is missing or empty, the gateway-wide policy is
invalid and Envoy answers every route with 500. Going from invalid back to
valid is not safe: on the trial cluster one request in that switch reached
ArgoCD without OIDC, because Envoy took the new route before the new
listener. So the Secret is created by the edge chart with a placeholder
value, before the policy, and the edge Application syncs with
`ApplyOutOfSyncOnly` and ignores its data, so a sync never resets the real
value. Changing the value (valid to valid) showed no gap.

Envoy Gateway also adds the OAuth2 filter to the listener only while at least
one route uses the gateway policy, so the first route after none would open
the same gap. A permanent route in `edge`
(`factory.jomcgi.dev/oauth2-sentinel`, 404 once signed in) keeps that count
above zero.

The proxy's egress to factory-auth.jomcgi.dev (the OIDC token endpoint) and
argocd-server's (discovery and keys) name a hostname that resolves to
Cloudflare's anycast addresses, so the rule admits any site Cloudflare serves
on those addresses.

To publish another hostname, change these together in one PR: the hostname
list in `projects/platform-factory/edge-tunnel/resources/route.yaml`, the
`factory-edge` provider's `redirect_uris` in
`projects/platform/authentik/factory-blueprints/factory-auth.yaml`, the app's
HTTPRoute on `edge/edge` and its namespace in `routeNamespaces`, and the
network policies above.

To take the edge down, delete the `edge-tunnel` Application first, so
nothing is published while the rest goes, then `authentik`,
`cloudnative-pg` and `edge`, and `edge-admission` last. If Envoy Gateway's
controller goes before its Gateway, the `edge` GatewayClass keeps its
finalizer; clear it by name. The admission policies and bindings, and
`authentik/authentik-secrets` (which holds `AUTHENTIK_SECRET_KEY`), carry
`Delete=false`, so deleting their Applications leaves them; delete them by
name once the edge is gone and the key is no longer wanted. The namespaces
and CRDs stay behind.

### Authentik first run

Authentik starts with no admin password, and the internet cannot reach the
setup flow. Set it up through a port-forward:

```bash
KUBECONFIG=~/.kube/factory.yaml kubectl -n authentik port-forward svc/authentik-server 9000:80
# open http://localhost:9000/if/flow/initial-setup/ and set akadmin's email and password
```

Then sign in at https://factory.jomcgi.dev. The first sign-in enrols a
second factor; choose TOTP. A WebAuthn key enrolled over the port-forward is
bound to `localhost` and fails on factory-auth.jomcgi.dev. The admin and user
interfaces are reachable only through the port-forward.

`authentik-secret-init` (`projects/platform-factory/authentik/resources/`)
fills `AUTHENTIK_SECRET_KEY` and both OIDC client secrets into
`authentik/authentik-secrets` once, and copies the client secrets to
`edge/edge-oidc-client` and `argocd/argocd-authentik-oidc`. ArgoCD creates
the three Secrets empty and ignores their data, so the Job can only read and
update those names. CloudNativePG generates `authentik-pg-app`. Move the
secret key to 1Password once Postgres has off-node backups.

### Break-glass

ArgoCD's local `admin` account is disabled. With the cluster kubeconfig,
use kubectl directly, or the ArgoCD CLI without its API server:

```bash
KUBECONFIG=~/.kube/factory.yaml kubectl config set-context --current --namespace argocd
KUBECONFIG=~/.kube/factory.yaml argocd app list --core
```

## Object storage

Garage (`projects/platform-factory/garage/`) runs as a three-pod StatefulSet,
one pod per node by required anti-affinity, each with local-path volumes for
metadata (1 GiB) and data. Replication factor 3 with `consistent` mode: reads
and writes need two of the three nodes, so it tolerates one node down. Each
node's zone is its pod name; the layout gives each 50 GB of capacity, which
balances data but is not enforced (local-path has no quotas). The manifests
are written by hand because the upstream Helm chart is published only in
Garage's Git repository and generates its RPC secret with Helm's `lookup` and
`randHex`.

Nodes find each other with Garage's Kubernetes discovery: each publishes a
`GarageNode` (CRD in `resources/crd.yaml`) with its pod IP when it starts, so
a restart with a new IP reconnects. `garage-init` (`resources/garage_init.py`)
does the rest, idempotently:

- **Secrets.** Before the StatefulSet, it fills `garage/garage-secrets` with
  the RPC secret and admin token, once. They exist only in the cluster; the
  Secret carries `Delete=false`.
- **Layout.** After every sync (PostSync Job) and hourly (CronJob), it waits
  for three connected nodes and applies the first layout if there is none.
  It never changes an existing layout: a node that comes back with a new ID
  (a lost metadata volume) is logged as a warning, and replacing it is an
  operator step (`garage layout assign`/`remove` from a Garage pod).
- **Buckets and keys.** It creates `factory-itsaplan`, `factory-evidence`,
  `ate-snapshots` and `kagent-pg-backups`. Each consuming namespace declares
  an empty Secret and a Role letting `garage/garage-init` get and update only
  that name; garage-init generates a key into the Secret
  (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_ENDPOINT_URL`,
  `AWS_REGION`, `BUCKET`, ready for `envFrom`), imports it into Garage if
  Garage lacks it, and grants read and write on that one bucket. The Secret
  is the source of truth, so a rebuilt Garage gets the same keys back. A
  consumer whose Secret appears after the last sync is served by the next
  hourly run.

| Bucket | Key Secret | Consumer |
| --- | --- | --- |
| `kagent-pg-backups` | `kagent/kagent-pg-s3` | `kagent-pg` WAL archive and base backups |
| `factory-itsaplan` | `factory/factory-itsaplan-s3` | itsaplan (step 6) |
| `factory-evidence` | `factory/factory-evidence-s3` | the connector's transcripts and CI logs (step 9) |
| `ate-snapshots` | none yet | Agent Substrate, when its snapshots move off rustfs |

`projects/platform-factory/factory-buckets/` holds the `factory` namespace's
key Secrets ahead of the workloads. The S3 API admits only `ate-system`,
`factory`, `kagent`, `cnpg-system` and CloudNativePG instance pods in any
namespace; RPC (3901) only Garage pods; the admin API (3903) only
garage-init. A consumer also needs its own egress rule to
`app.kubernetes.io/name=garage` in `garage` on 3900. Traffic is plain HTTP
inside the cluster; Cilium's WireGuard encrypts it between nodes.

The Application, the StatefulSet's PVCs (retention policy `Retain`) and the
key Secrets carry `Prune=false,Delete=false`, so removing the Application
leaves the data and keys. Check health with:

```bash
KUBECONFIG=~/.kube/factory.yaml kubectl -n garage exec garage-0 -- /garage -c /etc/garage/garage.toml status
KUBECONFIG=~/.kube/factory.yaml kubectl -n garage logs job/garage-bootstrap
```

## Postgres

CloudNativePG 1.30 runs every database. `kagent-pg`
(`projects/platform-factory/kagent-postgres/`) is kagent's: three instances,
one per node by required anti-affinity, each 256 MiB requested with a 1 GiB
limit. Three fit: the nodes have about 11 GiB free each, and idle instances
use under 100 MiB. Losing the primary promotes a replica in seconds; the
failover trial on #7817 kept every row.

It is a separate Application rather than part of the factory chart
(`postgres.mode: existing`), so the database's lifecycle and the chart's are
independent: a chart rollback or reinstall never touches the data, and the
Cluster, its logins and the Application carry `Prune=false,Delete=false`.
`kagent-pg-setup`, a PostSync Job, generates the two login passwords once
into `kagent-pg-logins`, writes the connection-string Secrets the chart reads
(`kagent-postgres-owner`, `kagent-postgres-readwrite`), and runs kagent
v1.0.1's own setup script (`setup.sql`, copied unchanged) as the superuser
to create `kagent_owner`, `kagent_readwrite`, their login users and schema
`kagent`. Only the kagent controller, the setup Job, peers and the operator
reach the instances. kagent reads the connection strings only at start, so
restart its controller (through ArgoCD) after they change.

Backups use the in-tree barman-cloud integration: continuous WAL archiving
and a daily base backup (02:30 UTC, 14 days kept) to `kagent-pg-backups` in
Garage. 1.30 deprecates it and 1.31 removes it; the barman-cloud plugin would
also need cert-manager, which the cluster does not run. The operator upgrade
to 1.31 must move this to the plugin first. Garage is on the same three
nodes, so these backups protect against losing a node, a volume or a bad
change, not against losing the cluster.

The itsaplan and connector databases come with the factory chart's
`postgres.mode: install` (steps 6 and 9), which knows their roles and
clients.

## Agent Substrate

The combination in use is Kubernetes `v1.37.2+k3s1`, Agent Substrate `1.0.1`
from the [kagent-dev fork](https://github.com/kagent-dev/substrate) and gVisor
`release-20261102.0`. This is the fork release line kagent v1.0.1 uses (it
builds against fork v1.0.0), not upstream agent-substrate/substrate. Substrate needs Kubernetes 1.37 or later with
`certificates.k8s.io/v1beta1` served. Its atelet downloads the gVisor release
named in the `gvisor-default` SandboxConfig (version `v7`, sha256-pinned) and
runs it inside each worker pod; the nodes' own runsc is not used by Substrate.

The fork publishes four Helm charts: `substrate-crds`, `substrate-podcert`,
`substrate-atelet` and `substrate`. They take one image tag for every
component, and ArgoCD here has no post-render step, so a Helm source could not
pin images by digest. The charts are rendered into Git instead:

- `projects/platform-factory/substrate/render-upstream.sh` pulls each chart
  at its version, checks its OCI digest, renders it with `values/` into
  `resources/upstream/`, and pins every image to its digest. It also copies
  the fork's bundled PostgreSQL and role script, which the charts leave out.
  To upgrade, rerun it and review the diff. Each version bump restarts
  postgres, because the role script's header changes the hash of its
  ConfigMap.
- `values/` holds the settings kagent needs. Both charts get the preview
  gates `PerTemplateSnapshotLocation` and `GoldenEgressPolicy`. The router's
  ingress allowlist and the API's bootstrap owners name
  `spiffe://cluster.local/ns/kagent/sa/kagent-controller`. The `k8s.io`
  credential provider grants the `kagent` atespace only those Secrets in the
  `kagent` namespace labelled `substrate.ate.dev/injectable=true`. atelet is
  its own release.
- `resources/` adds what the charts don't cover: S3 snapshots in an
  in-cluster rustfs (`snapshot-store.yaml`, bucket `ate-snapshots`, until
  Garage replaces it), the PostgreSQL connection-string Secrets (no
  passwords, since both logins use pod certificates), the role setup in a
  postStart hook, ate-api-server's authentication config and Cilium
  allowlists. OpenTelemetry push is off; Prometheus `/metrics` stays on.
  It deletes the chart's ClusterRoleBinding that lets the credential provider
  read every Secret in the cluster, and adds the `kagent-controller-*`
  ValidatingAdmissionPolicies (`kagent-identity-guard.yaml`, below).
- `bootstrap-substrate.sh` creates the generated Secrets with the fork's
  `kubectl-ate`: the podcertificate signer CAs, the actor identity CA and JWT
  keys, the egress MITM CA (`egress-mitm-ca-pool`, published to actors as the
  `egress-mitm.ate.dev` trust bundle) and the snapshot store's key. They
  exist only in the cluster, and the fork uses the same names and formats as
  upstream. The CAs expire on 2027-11-13, a year after creation. Substrate 1.0
  has no CA rotation command, so rotation is tracked in
  [#7831](https://github.com/jomcgi-org/homelab/issues/7831).

Control plane: `ate-system` (ate-api-server, ate-controller, ate-idp-server,
atenet-router and atenet-egress on agentgateway, k8s-credential-provider, the
atelet DaemonSet, postgres and rustfs) and `podcertificate-controller-system`.
WorkerPools may live in any namespace. The `substrate-workers` cluster-wide
policy lets their pods reach only the egress gateway. atelet reaches workers
over a unix socket on the node. ate-controller limits their ingress to
atenet-router. The gateway reaches ate-api-server and the credential
provider. The credential provider reaches only the Kubernetes API, and only
the gateway can reach it. Actors have no other egress until a destination is
allowed in `network-policy.yaml`.

kagent's installation (step 5 of `projects/factory/ARCHITECTURE.md`) must:

- create the `kagent` namespace labelled `ate.dev/api-access=true`, since
  Substrate's API admits only namespaces with that label;
- run the controller as ServiceAccount `kagent-controller`, synced by ArgoCD,
  and give it Cilium egress to `api.ate-system.svc:443` and the router;
- bind ClusterRole `k8s-credential-provider-secret-reader` to
  `ate-system/k8s-credential-provider` with a RoleBinding in `kagent`, the
  only namespace whose Secrets the provider can then read;
- label each Secret meant for injection `substrate.ate.dev/injectable=true`.

Substrate trusts `kagent/kagent-controller` before kagent exists, so the
`kagent-controller-*` ValidatingAdmissionPolicies stop anyone else from
claiming it. In namespace `kagent` they deny creating or updating
ServiceAccount `kagent-controller`, and Pods, Deployments and ReplicaSets that
run as it. Only ArgoCD's application controller is exempt, plus three
`kube-system` controllers acting for ArgoCD's Deployment: deployment-controller
and replicaset-controller create its ReplicaSets and Pods, and
generic-garbage-collector clears their finalizers when ArgoCD deletes it. An
admin's `kubectl rollout restart` of that Deployment is therefore denied by
design; restart it through ArgoCD. The policies don't cover `pods/exec` or
`pods/ephemeralcontainers`, so those must stay admin-only in `kagent`.

These broad grants are acceptable while only admins hold kubeconfigs. Revisit
them before actors get internet egress or agents get cluster credentials:

- `substrate-workers`, upstream's atenet-egress ingress policy and the
  router's egress all select any pod labelled `ate.dev/worker-pool`, in any
  namespace. Anyone who can create pods can claim the label.
- The shared `ate-system/ate-client` ServiceAccount is ate-api-server's
  bootstrap owner (`--authz-bootstrap-owners`, ate-setup's default), so
  anyone who can mint its token owns every atespace. `kubectl ate` uses it
  unless given `--token-file`.
- `kagent/kagent-controller` is a global owner too and may reach every actor
  through the router, so anything running as it controls all of Substrate.
  The admission policy above limits who can run as it.
- The `kagent` atespace may inject every labelled Secret in `kagent`, so a
  Secret labelled there is available to every kagent agent.

Verify an install:

```bash
export KUBECONFIG=~/.kube/factory.yaml
kubectl -n ate-system get pods          # all Running and Ready
kubectl get sandboxconfig gvisor-default # DEFAULT v7
kubectl get clustertrustbundles | grep -e podcert.ate.dev -e egress-mitm
# kubectl-ate from the same release, then a throwaway actor:
kubectl ate create atespace substrate-smoke
kubectl apply -f <WorkerPool in namespace substrate-smoke, workerImage ghcr.io/kagent-dev/substrate/ateworker-gvisor:v1.0.1@sha256:...>
kubectl ate --preview create actor-template -f <template>   # wait for GOLDEN TAG
kubectl ate create actor smoke-1 -a substrate-smoke --template smoke
kubectl ate proxy actor smoke-1 -a substrate-smoke # then send it a request
```

To check credential injection, give the actor an egress policy
(`kubectl ate create egress-policy`) with an `http` rule for an in-cluster
echo server. Its `replaceHeaders` effect should name an
`ate-secret://k8s.io/default/<namespace>/<secret>/<key>` that the actor's
atespace is granted in `values/substrate.yaml`: a Secret labelled
`substrate.ate.dev/injectable=true` in a namespace where the provider has a
RoleBinding. The gateway's own policy doesn't allow the echo server, so the
check also needs two temporary CiliumNetworkPolicies, deleted afterwards. One
in `ate-system` allows `app: atenet-egress` egress to the echo pod's port, and
one in the echo server's namespace allows ingress from it. The echo server
should receive the Secret's value, and the actor should hold only its
placeholder.

Check Hubble for drops while it runs (see Network policy). Then delete the
actor, the template, the atespace, the WorkerPool and the namespace.

## Observability

`projects/platform-factory/otel-collector/` deploys
`projects/platform/otel-collector` with `values.yaml` and
`values-factory.yaml`, plus its network policy. There is no trace gateway: nothing
on the cluster emits traces yet.

Everything goes to the Honeycomb environment `homelab`, the hub's, with the
hub's `honeycomb-ingest` key. The environment routes by its own rules, not by
the chart's dataset names:

| Data | Honeycomb dataset | Filter |
| --- | --- | --- |
| Node, pod, host, Cilium, Hubble and API server metrics | `metrics` (the environment's metrics dataset, shared with the hub) | `k8s.cluster.name = factory` |
| Pod logs, warning and above | one dataset per `service.name` (for example `coredns`), shared with the hub when names match | `k8s.cluster.name = factory` |
| Kubernetes Warning events | `k8s-events` | `k8s.cluster.name = factory` |

Useful starting points in the `metrics` dataset, all filtered on
`k8s.cluster.name = factory`:

- Node headroom: `MAX(k8s.node.memory.working_set)`, `MAX(k8s.node.cpu.usage)`
  and `MAX(system.memory.usage)` grouped by `k8s.node.name`.
- Busiest pods: `MAX(k8s.pod.memory.working_set)` grouped by
  `k8s.namespace.name` and `k8s.pod.name`.
- Policy drops: `SUM(hubble_drop_total)` grouped by `reason`,
  `source_namespace` and `traffic_direction`.
- Scrape health: `MIN(up)` grouped by `service.name` and
  `service.instance.id`; a target at 0 is unreachable.
- etcd: `MIN(kubernetes_healthcheck)` grouped by `name` and
  `service.instance.id` (etcd and etcd-readiness, per API server), and
  `MAX(apiserver_storage_size_bytes)`.

For logs, run an environment-wide query filtered on
`k8s.cluster.name = factory` and group by `service.name` and
`k8s.namespace.name`. etcd's own metrics listen on 127.0.0.1:2381 on each node,
out of a pod's reach, so etcd health comes from the API servers' readiness
checks instead.

What is left out or reduced before export:

- Logs from `authentik` and `ate-system` are not collected at all: sign-in
  details and agent session tokens do not leave the cluster. Every other line
  has bearer tokens, Authorization values and `password=`/`token=`/`secret=`
  style values masked, ANSI colour stripped, and identical repeats within a
  minute collapsed into one record with `log_count`. Nothing caps log volume
  outright; a noisy component needs its own fix or an exclusion.
- A repeating Warning event is sent when it first appears and when its count
  reaches 2, 4, 8, 16, 32, 64 and then every hundredth repeat.

The Cilium agent's metrics (:9962) and Hubble's (:9965) listen on every host
interface, `tailscale0` included, so any tailnet device that can reach node-1
can read them. They carry flow counts and drop reasons, not payloads.

The cluster agent is one replica with the `Recreate` strategy, so a rollout
never runs two Event watchers. That is not a guarantee: if a node is
partitioned, a replacement can start while the old pod still runs, and both
ship each Event until the old one stops.

## Not yet done

These come from the #7817 step 1 checklist:

- Durable Agent Substrate state: its PostgreSQL and snapshot store are on
  local-path volumes on one node each, with no backup.
- Actor internet egress and the credential broker. The credential provider
  runs, but grants only the `kagent` namespace.
- Off-site backup and restore: etcd snapshots with a copy of the cluster
  token, Garage's buckets and the CNPG archives, to R2 or GCS. Garage and
  `kagent-pg` replicate across the nodes but share the cluster's failure
  domain.
- Substrate's PostgreSQL and snapshots onto the replicated stores: its
  snapshots to Garage's `ate-snapshots` (grant `ate-system` a key), its
  database to CloudNativePG.
- Moving `kagent-pg` backups to the barman-cloud plugin (and cert-manager)
  before upgrading CloudNativePG to 1.31, which removes the in-tree
  integration.
- Agent Substrate CA rotation before 2027-11-13
  ([#7831](https://github.com/jomcgi-org/homelab/issues/7831)).
- Postgres backups for Authentik (Garage now available), then its secret key in 1Password.
- Spreading the cloudflared replicas across nodes: cfgate v0.2.0-alpha.11's
  CloudflareTunnel has no topologySpreadConstraints or affinity field, so
  placement is the scheduler's default spreading.
- Prometheus and Grafana as the operators' metrics view (#7817 names them);
  Honeycomb carries the baseline telemetry until then.
- Alerting on the factory telemetry. Honeycomb's single free trigger is taken
  (`projects/platform/honeycomb/README.md`).
