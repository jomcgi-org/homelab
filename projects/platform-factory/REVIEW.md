# Review: the factory cluster's platform

Rules for anything deployed to the factory cluster, on top of the root
`REVIEW.md`. The edge, Authentik and the admission policies are its trust
boundary: a rule below broken on them is critical.

- **Every route signs in.** Each HTTPRoute attaches to the `edge` Gateway
  under its gateway-wide Authentik OIDC policy or one of the route-level
  SecurityPolicies below. There are only three:
  - Authentik's own route.
  - `kagent-authentik-oidc`, which carries the shared `edge.oidc` settings
    plus `forwardAccessToken: true`.
  - `kube-api-authentik-jwt` on the `kube-api` route
    (factory-kube.jomcgi.dev). kubectl cannot follow the browser redirect,
    so this policy checks a bearer token instead: one JWT provider with the
    issuer, audience and `/jwks/` of the Authentik client `factory-kubectl`
    on factory-auth.jomcgi.dev, and `authorization.defaultAction: Deny` with
    one allow rule on the `groups` claim. The route has no filters. Its only
    backend is `default/kubernetes:443`, with a BackendTLSPolicy that checks
    the certificate against `kube-root-ca.crt`, and its ReferenceGrant admits
    only HTTPRoutes from `edge`. The API server checks the token again
    ([Joe's decision](https://github.com/jomcgi-org/homelab/pull/8237#issuecomment-6975000416)).

  `test_only_kagent_forwards_the_access_token` in
  `projects/platform-factory/edge/chart/edge_test.py` pins this set of
  policies. Until the `kube-api` route lands (#8222), the set it pins has
  only the first two. #8222 adds the third to that set and adds a test that
  pins the bearer policy, route, grant and TLS check.
  The `oidc-sentinel` route stays.
  Authentik's admin, user, API and initial-setup paths keep answering 403
  publicly.
- **Hostnames are owned.** A new route's namespace gets a rule in
  `edge-admission` naming that namespace and its exact hostnames, and the
  cfgate tunnel names the hostname. Each policy keeps `failurePolicy: Fail`
  and its `Delete=false` annotation.
- **OIDC redirect URIs are exact**: no wildcards, and none left on a hostname
  that moved.
- **Public output.** No HTTPRoute is public. Public output leaves only
  through the jomcgi-site Worker. A new `sources.json` section or
  `edge/routes.json` pattern is Joe's decision. A publisher of factory data
  uses a field allowlist and a secret scan.
- **Application charts.** A workload renders through `factory-library.all`.
  Each `exceptions:` entry states its reason. High.
- **Own namespace only.** No template sets a namespace other than the release
  namespace. No cluster-scoped kind. No Role beyond named resources. No
  ClusterRole with Secret, workload-create or RBAC-write rights. Critical.
- **Host access.** No `privileged`, `hostPID`, `hostNetwork` or hostPath
  outside the named platform namespaces. Critical.
- **Images.** Every image names a digest. High.
- **Secret items.** The PR's issue names each new or changed `itemPath` and
  each `OnePasswordItem` in a new namespace. Two stages never share a
  credential. High.
- **Egress credentials.** The origin of an injected credential does not echo
  headers, host user content or use a wildcard. A new pod in the kagent
  controller's ingress list verifies the bearer first. Critical.
- **Renovate.** A `renovate.json` change that widens automerge or shortens
  the release age is Joe's decision.
- **Network policy.** A new workload declares its ingress and egress by peer
  label, port and domain name; the cluster is default-deny. Egress to the
  world or to `0.0.0.0/0` is high.
- **Private images.** A namespace running a private `ghcr.io/jomcgi` image
  has the GHCR pull secret and the pod names it: nodes hold no ghcr
  credentials and Spegel does not mirror ghcr.io (#7894, #7914).
- **Kept resources.** `Prune=false`, `Delete=false`,
  `helm.sh/resource-policy: keep` and `ignoreDifferences` on Secrets written
  at runtime stay unless the PR's issue says to remove them. Without them
  the next sync deletes state or reverts a grant. Critical.
- **Secrets** come from the 1Password operator or are generated in the
  cluster. The repository is public.
