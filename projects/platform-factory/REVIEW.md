# Review: the factory cluster's platform

Rules for anything deployed to the factory cluster, on top of the root
`REVIEW.md`. The edge, Authentik and the admission policies are its trust
boundary: a rule below broken on them is critical.

- **Every route signs in.** Each HTTPRoute attaches to the `edge` Gateway
  under its gateway-wide Authentik OIDC policy or one of the route-level
  SecurityPolicies below. There are only four:
  - Authentik's own route.
  - `kagent-authentik-oidc`, which carries the shared `edge.oidc` settings
    plus `forwardAccessToken: true`.
  - `codex-admin-authentik-oidc`, which forwards the access token with the
    shared `edge.oidc` settings on the broker’s `/admin/codex` route.
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

  In `projects/platform-factory/edge/chart/edge_test.py`,
  `test_only_kagent_forwards_the_access_token` pins the deployed edge
  policies, and `test_kube_api_requires_an_authentik_token_from_a_kube_group`
  pins the kube-api bearer policy, route, grant and TLS check.

  The Codex policy is reserved for a follow-up after the broker listener, ingress
  policy and exact Authentik callback are deployed. Its redirect URL must
  be `https://factory.jomcgi.dev/admin/codex/oauth2/callback` so the same
  policy handles sign-in and callback. Couple route enablement to the broker
  chart pin so rollback also withdraws the route. Publish the alert links
  after verifying a fresh browser login.
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
