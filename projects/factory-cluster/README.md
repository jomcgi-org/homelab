# Factory home cluster

The home Kubernetes cluster that hosts the replacement software factory
([#7817](https://github.com/jomcgi-org/homelab/issues/7817)). It is being
built before any workload moves from the GKE hub. This directory holds its node
bootstrap and its ArgoCD root; `projects/platform-factory/` holds one
Application per shared component.

Status (2026-11-13): nodes rebuilt as three k3s servers on Cilium; 1Password
Connect and ArgoCD run and sync `factory-root`. No hostname is published.

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
| Storage | local-path (k3s default) | Enough for bootstrap; replicated storage comes with the first stateful service |
| Images | embedded Spegel mirror for every registry | Each image crosses the home uplink once |
| Cilium devices | `enp+` (the wired NICs) | Auto-detection also took `tailscale0` on node-1, set every pod there to its 1280 MTU and silently dropped full-size QUIC replies from the Cloudflare edge; it also served KPR services on the tailnet |
| Tailscale (node-1) | netfilter mode off; `src_valid_mark=0` | With Tailscale managing iptables it sets `src_valid_mark=1`, and the kernel then drops every pod packet Cilium redirects to its DNS or L7 proxy, which breaks `toFQDNs` policies |
| Ingress | Cloudflare Tunnel managed by cfgate (Gateway API) | No inbound ports; the tunnel, its routes and DNS records are Kubernetes resources. cfgate replaced StringKe/cloudflare-operator, whose last release (v0.51.0) watches Gateway API `v1alpha2` UDPRoute, which current Gateway API no longer serves |
| Human sign-in | Authentik OIDC at each application, no Cloudflare Access | One identity layer, as #7817 specifies; nothing is published until it signs users in |
| Secrets | 1Password Connect + operator, `k8s-homelab` vault | Repository convention |
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
that Application (cfgate does), so the policy lands with the namespace. A
component with no policy can reach nothing, so a new component needs one in
the same change that deploys it.

To see what a policy drops:

```bash
KUBECONFIG=~/.kube/factory.yaml kubectl -n kube-system exec ds/cilium -c cilium-agent -- \
  hubble observe --verdict DROPPED -f
```

That shows one node; run it against each agent pod for the whole cluster.

## factory.jomcgi.dev

`projects/platform-factory/factory-edge/` declares the only path in from the
internet: the `factory` CloudflareTunnel, the `cloudflare-tunnel` Gateway that
accepts HTTPRoutes only from namespaces labelled `factory.jomcgi.dev/edge=true`,
and a CloudflareDNS that creates records only for routes annotated
`factory.jomcgi.dev/publish=true`. No namespace has the label yet, so nothing
is published and every request answers 404.

The authenticated ingress (Envoy Gateway with Authentik forward-auth) will be
the one edge namespace, so an application cannot publish itself by writing an
HTTPRoute. factory.jomcgi.dev is published with it. Cloudflare Access is not
used. Until then, reach ArgoCD through a
port-forward and sign in as `admin` with the password in the
`argocd-initial-admin-secret` Secret:

```bash
kubectl --kubeconfig ~/.kube/factory.yaml -n argocd port-forward svc/argocd-server 8080:80
```

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
run as it. Only ArgoCD's application controller is exempt, plus the
`kube-system` deployment-controller and replicaset-controller, which create the
ReplicaSets and Pods for ArgoCD's Deployment.

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
- Replicated storage, and backup and restore: off-node etcd snapshots with a
  copy of the cluster token, and Substrate's PostgreSQL and snapshots, the
  first state here that Git cannot recreate.
- Agent Substrate CA rotation before 2027-11-13
  ([#7831](https://github.com/jomcgi-org/homelab/issues/7831)).
- Authentik for the factory cluster, the Envoy Gateway edge with forward-auth,
  and factory.jomcgi.dev.
- Prometheus and Grafana as the operators' metrics view (#7817 names them);
  Honeycomb carries the baseline telemetry until then.
- Alerting on the factory telemetry. Honeycomb's single free trigger is taken
  (`projects/platform/honeycomb/README.md`).
