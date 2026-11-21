# Review: the factory cluster's platform

Rules for anything deployed to the factory cluster, on top of the root
`REVIEW.md`. The edge, Authentik and the admission policies are its trust
boundary: a rule below broken on them is critical.

- **Every route signs in.** Each HTTPRoute attaches to the `edge` Gateway
  under its gateway-wide Authentik OIDC policy or one of the route-level
  SecurityPolicies below. The Codex policy is reserved for HOME-405.
  - Authentik's own route.
  - `kagent-authentik-oidc`, which carries the shared `edge.oidc` settings
    plus `forwardAccessToken: true`.
  - `codex-admin-authentik-oidc`, which forwards the access token with the
    shared `edge.oidc` settings on the broker’s `/admin/codex` route.
  - `t3-authentik-oidc` targets only the `t3` route on `t3.jomcgi.dev`.
    It uses the shared Authentik OIDC settings with the exact T3 callback,
    requires MFA and membership in the Authentik admin group, and denies
    other groups. Its sole backend is `t3/t3-relay:8080`. The Gateway policy
    must deny T3 if the route policy is absent. Publish the hostname only
    after the admission guard, node-1 socket and relay are verified live
    ([HOME-681](https://factory-plan.jomcgi.dev/factory/issue/HOME-681)).
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
  - `scotland-connector-public` on the `scotland-connector` route
    (scotland-mcp.jomcgi.dev), the one public route. It has no sign-in:
    `authorization.defaultAction: Allow` with the hub's two deny rules for
    Cloudflare Worker subrequests. The route keeps the hub's rate limits.
    `chart_posture_test.py` in `projects/scotland-connector/chart` pins both
    ([Joe's decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-259#comment-3908)).

  - `explore-api-authentik-jwt` admits tokens from the factory-explore client
    to explore-proxy. `test_explore_api_requires_explorer_token` pins its
    provider, route, grant, CORS and circuit breaker.
    [Joe’s decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-352#comment-3461).

  In `projects/platform-factory/edge/chart/edge_test.py`,
  `test_only_kagent_forwards_the_access_token` pins the edge chart policies,
  and `test_kube_api_requires_an_authentik_token_from_a_kube_group` pins the
  bearer policy, route, grant and TLS check.


  The optional `discord-activity` policy on bosun-activity-api.jomcgi.dev
  delegates Discord sign-in to the bridge. Its POST token endpoint exchanges
  a Discord authorization code. Its instance GET and POST endpoints require
  a bearer token and a successful Discord membership lookup for the launch
  guild. The factory chart creates the policy with the Activity route.
  [HOME-170](https://factory-plan.jomcgi.dev/factory/issue/HOME-170).

  The Codex policy is reserved for a follow-up after the broker listener, ingress
  policy and exact Authentik callback are deployed. Its redirect URL must
  be `https://factory.jomcgi.dev/admin/codex/oauth2/callback` so the same
  policy handles sign-in and callback. Couple route enablement to the broker
  chart pin so rollback also withdraws the route. The Codex quota and grant
  alerts already link the page; the link answers only after the route is
  enabled. [Joe staged the route](https://factory-plan.jomcgi.dev/factory/issue/HOME-323#comment-4035)
  to HOME-405.
  The `oidc-sentinel` route stays.
  Authentik's admin, user, API and initial-setup paths keep answering 403
  publicly.
- **Hostnames are owned.** A new route's namespace gets a rule in
  `edge-admission` naming that namespace and its exact hostnames. The cfgate
  tunnel names each published hostname. Each policy keeps `failurePolicy: Fail`
  and its `Delete=false` annotation.
- **OIDC redirect URIs are exact**: no wildcards, and none left on a hostname
  that moved.
- **Public routes are Joe's decision** (#7923). The only one is the Scotland
  connector at scotland-mcp.jomcgi.dev, which serves public forecast and
  astronomy data from the cluster with no secrets in its responses
  ([decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-259#comment-3908)).
  Any other public route serves only mock or fixture data cached by the CDN,
  with no secrets and no calls into the cluster.
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
