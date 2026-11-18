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
    ([Joe's decision](https://github.com/jomcgi-org/homelab/pull/8236#issuecomment-6974854609)).

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
- **No public routes yet.** A route outside sign-in is Joe's decision
  (#7923). When one exists it serves only mock or fixture data cached by the
  CDN, with no secrets and no calls into the cluster.
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
