# Factory credentials

External Secrets Operator (ESO) runs in `external-secrets`. Its upstream chart
is pinned to `2.15.0`, including CRDs, the `GithubAccessToken` and `Webhook`
generators, and the Kubernetes provider. The Application follows 1Password at
wave 1. Its local chart installs `factory-credentials` at wave -2 and the
admission guards and Cilium policies at wave -1, before ESO's deployments.

This slice installs no root Secrets, generators, ExternalSecrets, consumer
grants or workloads in `factory-credentials`. The factory broker continues
minting GitHub tokens as described in
[its architecture](../../factory/ARCHITECTURE.md#credential-broker).

## Keep roots apart from consumers

The 1Password Operator supplies root secrets to `factory-credentials`.
Generators and the ExternalSecrets that invoke them must live beside those
roots: ESO resolves generator secret references in the ExternalSecret's
namespace and ignores an explicit namespace on the reference. See the
[GitHub generator documentation](https://external-secrets.io/latest/api/generator/github/)
and [Joe's decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-67#comment-425).

Each consumer later gets a namespaced `SecretStore` using ESO's
[Kubernetes provider](https://external-secrets.io/latest/provider/kubernetes/)
with `remoteNamespace: factory-credentials`. It authenticates as a dedicated
ServiceAccount in the consumer namespace. A RoleBinding in
`factory-credentials` gives that identity a Role with `verbs: [get]` and
`resourceNames` naming only that consumer's minted Secret. The consumer's
ExternalSecret copies that Secret into its own namespace by explicit
`data[].remoteRef.key`. Root secrets never appear in a consumer grant. Do not
use discovery (`dataFrom.find`), which needs broader list access.

When using `auth.serviceAccount`, add a Role in the consumer namespace giving
ESO `create` on `serviceaccounts/token`, limited by `resourceNames` to that
ServiceAccount, and bind it to `external-secrets/external-secrets`. Global
ServiceAccount token creation is disabled in this installation. Use the local
`kube-root-ca.crt` ConfigMap for the Kubernetes provider's CA, and retain the
ServiceAccount's access to `SelfSubjectRulesReview` for store validation.

The API server rejects Pods in `factory-credentials`, GitHub and Webhook
generators outside it, and GitHub generators with absent or empty repositories
or permissions. Roles there can grant secret access only by name and only
with `get`; wildcard API groups and resources also count as secret access.
ClusterGenerator, ClusterSecretStore, ClusterExternalSecret, PushSecret and
ClusterPushSecret are rejected everywhere, and their reconcilers are disabled.
Every guard fails closed and retains its policy and binding on Application
deletion. The namespace is also protected from pruning and deletion.

ESO and 1Password remain trusted cluster controllers. Kubernetes RBAC must
keep consumers from authoring minting resources or changing ClusterRoles and
RoleBindings; the Role admission rule does not replace that authorization.
The built-in view/edit/admin roles receive no aggregated ESO permissions.

Cilium allows ESO egress only to the API server (TCP 443/6443) and webhook
ingress only from the API server (TCP 10250). The cluster baseline supplies
DNS to CoreDNS. There is no internet egress allowance in this slice.
Argo CD's repo-server separately allows HTTPS to `charts.external-secrets.io`
to fetch the upstream chart, alongside its existing GitHub release asset access.

## Add the next consumers

Slice 2 adds the R2 parent token through a OnePasswordItem, a Webhook generator
and its minting ExternalSecret, all in `factory-credentials`. It adds
`api.cloudflare.com:443` egress to the ESO controller and a consumer Role,
RoleBinding, ServiceAccount, SecretStore and copying ExternalSecret. Grant
only the consumer's minted temporary credential Secret.

Slice 3 follows the same pattern for GitHub App private keys and
GithubAccessToken generators, each with explicit repositories and permissions.
It adds `api.github.com:443` egress to the controller and switches each
consumer from the broker after its copied Secret refreshes successfully.
Set refresh intervals below credential expiry and preserve any target Secret
names, keys and labels required by the consumer. Those slices own the broker
cutover and removal of its old root-secret mounts.

The `AGENTS.md` Boundaries change requires Joe's **APPROVED** review of the
reviewed PR head under root `REVIEW.md` "Joe decides".

## Validate the change

From the repository root, after the sandbox toolchain bootstrap:

```bash
source "${XDG_CACHE_HOME:-$HOME/.cache}/homelab-sandbox-tools/env.sh"
ci lint
ci regen
ci ratchet
helm template external-secrets projects/platform-factory/external-secrets/chart \
  --namespace external-secrets
HELM_BIN="$(command -v helm)" python3 -m pytest -q \
  projects/platform-factory/external-secrets/chart/external_secrets_test.py
helm template external-secrets external-secrets \
  --repo https://charts.external-secrets.io --version 2.15.0 \
  --namespace external-secrets \
  -f projects/platform-factory/external-secrets/values.yaml
```

The render test has a Bazel target at
`//projects/platform-factory/external-secrets/chart:external_secrets_test`.
It also verifies that Argo CD can reach the chart repository through its
network policy.

`//projects/platform-factory/external-secrets/admission:admission_test` runs the
rendered guards against a private Kubernetes 1.37 API server and etcd using
envtest. Bazel pins the control-plane binaries and the released ESO chart by
checksum. The test verifies the chart version matches the Application and
installs its real CRD schemas, with the deployment's values. It never starts
ESO controllers or uses a kubeconfig or real credentials.

The test first verifies every fixture is schema-valid without the guards,
then installs all five policies and their bindings. It checks both CREATE and
UPDATE admission for every allowed and denied fixture, including widening
existing Roles and GitHub scopes. Additional cases cover empty repository and
permission collections and entries, wildcard Role rules, and forbidden
verbs. A denial must name the expected policy and its validation message;
schema, RBAC and CEL evaluation errors cannot count as success. Missing tools
or an API server startup failure fail the test instead of skipping it.

BUILD generation and these tests run in PR CI. With the repository's normal
Bazel test environment, the scoped admission command is:

```bash
bazel test //projects/platform-factory/external-secrets/admission:admission_test \
  --test_output=errors
```

## Verify after rollout

Run against the factory cluster after merge and ArgoCD sync. The Application
must report `Synced Healthy`, all three deployments must roll out, and the
admission policies must have no `expressionWarnings`. Use an identity allowed
to create these resource kinds so RBAC does not mask an admission failure.

```bash
export KUBECONFIG="$HOME/.kube/factory.yaml"
kubectl -n argocd get application external-secrets \
  -o jsonpath='{.status.sync.status}{" "}{.status.health.status}{"\n"}'
kubectl -n external-secrets rollout status deployment/external-secrets --timeout=180s
kubectl -n external-secrets rollout status deployment/external-secrets-webhook --timeout=180s
kubectl -n external-secrets rollout status deployment/external-secrets-cert-controller --timeout=180s
kubectl get validatingadmissionpolicies \
  factory-credentials-no-pods factory-credentials-generators \
  factory-credentials-github-scope factory-credentials-no-cluster-or-push \
  factory-credentials-secret-readers \
  -o jsonpath='{range .items[*]}{.metadata.name}{": "}{.status.typeChecking.expressionWarnings}{"\n"}{end}'
kubectl apply --dry-run=server \
  -f projects/platform-factory/external-secrets/checks/allowed.yaml
kubectl apply --dry-run=server \
  -f projects/platform-factory/external-secrets/checks/denied.yaml
```

All five resources in `checks/allowed.yaml` must succeed: a scoped GitHub
generator, a named get-only Role, a Webhook in `factory-credentials`, and a
Pod and broader Role outside it. All twelve resources in `checks/denied.yaml`
must be refused by the following admission policies. A missing CRD, schema error,
webhook timeout or RBAC denial does not pass this check. The last command is
expected to exit nonzero. The fixture commands use dry-run; they create no resources
and need no real App key.

| Manifest name | Expected policy (prefix `factory-credentials-`) |
| --- | --- |
| `home-70-denied-pod` | `no-pods` |
| `home-70-denied-no-repositories` | `github-scope` |
| `home-70-denied-no-permissions` | `github-scope` |
| `home-70-denied-outside-namespace` | `generators` |
| `home-70-denied-webhook` | `generators` |
| `home-70-denied-cluster-generator` | `no-cluster-or-push` |
| `home-70-denied-list` | `secret-readers` |
| `home-70-denied-unnamed-read` | `secret-readers` |
| `home-70-denied-cluster-store` | `no-cluster-or-push` |
| `home-70-denied-cluster-external` | `no-cluster-or-push` |
| `home-70-denied-push` | `no-cluster-or-push` |
| `home-70-denied-cluster-push` | `no-cluster-or-push` |

The `checks/` fixtures sit outside both Application source paths. ArgoCD never
applies them. Paste the commands and manifest list above into the PR body for
the rollout owner.
