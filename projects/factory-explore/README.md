# Explorer API rollout

See the [explorer architecture](ARCHITECTURE.md) for the components and
their trust boundaries.

HOME-356 stages the in-cluster Service `explore-proxy.factory-explore.svc:8080`.
The Application in `projects/platform-factory/kustomization.yaml` installs the
proxy before public routing. Its NetworkPolicy admits only the edge gateway.

Before activating HOME-358:

1. Verify that Authentik has applied the HOME-355 blueprint from
   `projects/platform/authentik/factory-blueprints/factory-explore.yaml`.
   Confirm a signed explorer token has issuer
   `https://factory-auth.jomcgi.dev/application/o/factory-explore/`, audience
   `factory-explore`, and a stable subject. Verify its RS256 signature against
   the provider's published JWKS.
2. Install a matching explorer JWT authenticator in
   `projects/factory-cluster/nodes/authentication-config.yaml`. The proxy
   passes the visitor's bearer token to Kubernetes. Map explorer identities
   into a dedicated prefix and bind only the intended explorer read permissions.
   Verify with an explorer token that a self-subject review returns the expected
   identity, an allowed read succeeds and a restricted read is denied.
3. In HOME-358, grant the edge gateway egress to the proxy on TCP 8080 and
   route `factory-explore-api.jomcgi.dev` through the authenticated edge to
   that Service, preserving the explorer bearer. Keep the operator kubectl
   hostname and its issuer separate.
4. Through the explorer hostname, verify the first read returns
   `X-Explore-Window-End`, a running watch closes at that deadline, and the
   next read returns 403 with `X-Explore-Next-Window`.
