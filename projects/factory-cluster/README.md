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
| node-1 | 192.168.4.80 (`enp1s0`) | 12 x86_64, VT-x | 16 GiB | 256 GB KIOXIA NVMe (OS + data) | Host `cloudflared` (bastion.jomcgi.dev SSH, obsidian) and Tailscale; operator workstation |
| node-2 | 192.168.4.81 (`enp2s0`) | 12 x86_64, VT-x | 16 GiB | 256 GB KIOXIA NVMe; 112 GB Hitachi HDD `sda` (NTFS, unused) | |
| node-3 | 192.168.4.76 (`enp2s0`) | 12 x86_64, VT-x | 16 GiB | 256 GB SK hynix NVMe | |

All three run Ubuntu 24.04.3 with kernel 6.8.0-138 and have `/dev/kvm`. The
retired home k3s was crash-looping without etcd quorum. Its join address was
192.168.1.161, and the LAN is now 192.168.4.0/22. `nodes/reset-node.sh` removed it in place:
k3s, `/var/lib/{rancher,longhorn,kubelet,embervm}` and the EmberVM scratch
loop mount. The OS, SSH, Tailscale and node-1's host `cloudflared` stayed.
node-2's `sda` was not touched.

The addresses are DHCP leases and embedded etcd records peer IPs, so the
router must reserve these three leases.

## Cluster

| Choice | Value | Why |
| --- | --- | --- |
| Distribution | k3s `v1.37.2+k3s1`, three servers, embedded etcd | In-place on the existing Ubuntu installs; tolerates one node loss. Agent Substrate needs Kubernetes 1.37+ |
| Certificates API | `certificates.k8s.io/v1beta1` served | Substrate's podcertificate controller uses the v1beta1 ClusterTrustBundle and PodCertificateRequest clients |
| Sandboxing | gVisor `release-20261102.0` (`runsc`), RuntimeClass `gvisor` | The same release Substrate's SandboxConfig pins. Substrate stages its own runsc inside worker pods; the RuntimeClass serves other sandboxed pods |
| Node sysctls | proxy ARP/NDP, larger neighbour table, inotify | Substrate's gVisor networking and sandbox density (`nodes/99-factory.conf`) |
| Network | Cilium 1.20.4: kube-proxy replacement, WireGuard pod encryption, Hubble | Cluster-wide default-deny and egress allowlists by domain name; Hubble shows what was dropped. Cilium lists Kubernetes 1.33 to 1.36 as tested, so 1.37 is validated here with `cilium connectivity test` |
| Storage | local-path (k3s default) | Enough for bootstrap; replicated storage comes with the first stateful service |
| Images | embedded Spegel mirror for every registry | Each image crosses the home uplink once |
| Ingress | Cloudflare Tunnel managed by cfgate (Gateway API) | No inbound ports; the tunnel, its routes and DNS records are Kubernetes resources. cfgate replaced StringKe/cloudflare-operator, whose last release (v0.51.0) watches Gateway API `v1alpha2` UDPRoute, which current Gateway API no longer serves |
| Human sign-in | Authentik OIDC at each application, no Cloudflare Access | One identity layer, as #7817 specifies; nothing is published until it signs users in |
| Secrets | 1Password Connect + operator, `k8s-homelab` vault | Repository convention |
| etcd snapshots | every 6 hours, 20 kept, on each node's disk | Off-node copies come later |

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
```

Nodes register after step 2 but stay NotReady: there is no CNI yet. Step 3
installs Cilium first, then creates the two 1Password bootstrap Secrets,
installs Connect and ArgoCD from this repository's charts, and applies
`factory-root-application.yaml`. From then on, merged changes under
`projects/platform-factory/` deploy automatically, and ArgoCD adopts the
Cilium, Connect and ArgoCD installs.

To upgrade k3s or gVisor, change the pins in `nodes/install-node.sh` and rerun
step 2. It restarts one node at a time.

The cluster holds no state that Git cannot recreate yet, so a lost cluster is
rebuilt with steps 2 and 3. Snapshots are encrypted with the cluster token on
each server (`/var/lib/rancher/k3s/server/token`); keeping a copy off the nodes
is part of the backup and restore work below.

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

Every pod is default-deny in both directions
(`projects/platform-factory/network-policy/resources/baseline.yaml`). The only
flow the baseline allows is DNS to CoreDNS, through Cilium's DNS proxy so that
egress rules can name domains. Each namespace then has one file allowing
exactly its own traffic, with internet egress listed by domain name. A
component with no file can reach nothing, so a new component needs one in the
same change that deploys it.

To see what a policy drops:

```bash
cilium hubble port-forward &
hubble observe --verdict DROPPED -f
```

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

## Not yet done

These come from the #7817 step 1 checklist:

- Agent Substrate install (`ate-setup`) and a recorded Actor start, write,
  suspend and resume. It also needs Postgres and S3-compatible snapshot storage.
- Replicated storage, and backup and restore: off-node etcd snapshots with a
  copy of the cluster token, once the cluster holds state Git cannot recreate.
- Authentik for the factory cluster, the Envoy Gateway edge with forward-auth,
  and factory.jomcgi.dev.
- Observability (OpenTelemetry, Prometheus/Grafana).
