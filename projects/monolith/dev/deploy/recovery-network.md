# Recovery network and credential preparation

`recovery-network-policy.yaml` and `recovery-secrets.yaml` are inactive inputs
for the [selected backend-loss proof](recovery-gke.md). No Application or
kustomization includes them. They target only `monolith-dev` and `embervm-dev`
on the GKE hub. Apply them before creating recovery workloads, after checking
namespace ownership and reviewing the complete activation operation.

The policy file supplies default deny in both directions for both namespaces,
then permits these pod connections using namespace and pod labels together:

| Source | Destination | TCP ports | Purpose |
| --- | --- | --- | --- |
| Recovery backend | Recovery PostgreSQL | 5432 | Application state, leader lease and DBOS |
| Recovery backend | Recovery Ember control plane and broker | 8080 | Session lifecycle and isolated provider login |
| Recovery Ember control plane | Recovery PostgreSQL | 5432 | Durable Ember operation log |
| Recovery Ember control plane | Recovery noded | 9090 | Guest scheduling and lifecycle RPCs |
| Recovery noded and its guest proxy | Recovery control plane and broker | 8080 | Registration, control callbacks and token exchange |
| Recovery noded and its guest proxy | Recovery backend | 8000, 8091 | Authenticated MCP and progress ingestion |
| CNPG operator | Recovery PostgreSQL | 8000, 5432 | Instance management and database access |
| Recovery PostgreSQL | Other instances of the same recovery cluster | 5432 | CNPG instance lifecycle |

DNS is permitted on UDP/TCP 53 to the observed resolver `10.10.16.10` and
`kube-system` pods labelled `k8s-app: kube-dns`. Only the Ember control plane,
broker and PostgreSQL pods may reach the private Kubernetes API endpoint,
`10.10.0.2:443`. The backend's disabled Kubernetes capabilities and the noded
process require no such grant. A projected service-account token does not
itself require pod access to the API. The kubelet supplies mounted Secrets.

The backend, control plane, broker and noded may use public TCP 443 for
authentication, provider calls and artifact/repository reads. Private,
loopback, link-local, shared-address and multicast ranges are excluded from
that rule. PostgreSQL has no general public egress. GKE Dataplane V2 does not
match pod traffic through `ipBlock`, so pod connections use explicit selectors.
The CNPG operator namespace and its webhook policies are not changed here.
This fresh-session profile uses dedicated credentials and no object store or
GKE Workload Identity token exchange. Metadata-server access is deliberately
denied; a future component requiring it needs an explicit reviewed grant.

This is **not a public hostname allowlist**. GKE FQDN policies are not enabled
on the observed hub, and the existing guest proxy intentionally permits public
internet destinations while restricting internal ones. Public production
URLs can still be reached over HTTPS; authentication and the dedicated recovery
issuer/audience remain necessary boundaries. Do not describe these policies as
complete isolation from every production service or enable a cluster-wide
network feature as a prerequisite for this proof.

Kubelet probes originate from the local node. Kubernetes NetworkPolicy allows
local-node traffic, and it does not sandbox a privileged noded process with
host access. These rules constrain ordinary pod traffic. They add no peer
access to frontend port 3000, metrics, serving activators or guest serving port
ranges, which the selected profile does not exercise.

## Secret delivery

The installed 1Password Operator 1.9.1 accepts a top-level `type` field and
copies the OnePasswordItem's labels to its Secret. This lets the fresh CNPG
credential be operator-owned without the manual copy described by the older
production procedure. The two resources supplied here are:

| Namespace / Secret | Dedicated item | Required fields |
| --- | --- | --- |
| `monolith-dev/monolith-dev-pg-embervm-oplog` | `embervm-recovery-oplog-db` | `username` equal to `embervm`, nonempty `password` |
| `embervm-dev/embervm-authentik-recovery-agent` | `embervm-authentik-recovery-agent` | Nonempty `app-password` for `kg-agent-recovery-sa` |

Keep values in 1Password. The Ember chart already creates its own op-log copy
from the first item, as well as the noded, registry and broker-grant resources.
Do not duplicate those resources or copy production refresh tokens. Before
CNPG bootstrap, verify the generated Secret's owner, basic-auth type,
`cnpg.io/reload` label and required key names without logging secret values.
Verify both op-log consumers join the same dev role, database and password.
The static test joins the broker's expected Secret name and `app-password`
key to the Ember preset. Only activation can verify that the actual 1Password
item and generated Secret contain that nonempty key for the intended principal.
Stop on an existing Secret or owner conflict: the Operator refuses to change
an existing Secret's type. This profile creates fresh state.

## Activation checks

Revalidate Dataplane V2, IPv4 address ranges, the API Endpoints backend, pod
resolver, DNS pod labels and CNPG operator labels on the actual cluster. Render
the exact published recovery charts and check every selector against their
pod templates and the CNPG-generated pod labels. Both namespaces must be unused
and have no other policies granting broader access: NetworkPolicy allows are
additive. Wait for policy and Secret reconciliation before starting the proof.

Before guest admission, verify DNS, database readiness, API access from its
three allowed consumers, control-plane-to-noded RPCs, provider login and the
actual guest MCP/progress route. Verify denied connections to production pod
destinations, unexpected peers, database management from a backend, and the
API from backend/noded. Record source pod identities, destinations and results.
An isolated pod probe alone does not prove the guest proxy/NAT path. A policy
failure stops admission; diagnose the specific missing path before changing
the reviewed rules. Static checks do not establish dataplane enforcement.

Source contracts: [GKE NetworkPolicy](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/network-policy),
[Kubernetes NetworkPolicy](https://kubernetes.io/docs/concepts/services-networking/network-policies/),
[CNPG 1.30 networking](https://github.com/cloudnative-pg/cloudnative-pg/blob/v1.30.0/docs/src/networking.md),
[1Password Operator 1.9.1 controller](https://github.com/1Password/onepassword-operator/blob/v1.9.1/internal/controller/onepassworditem_controller.go),
and [Secret construction](https://github.com/1Password/onepassword-operator/blob/v1.9.1/pkg/kubernetessecrets/kubernetes_secrets_builder.go).
