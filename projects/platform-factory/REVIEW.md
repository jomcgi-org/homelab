# Review: the factory cluster's platform

Rules for `projects/platform-factory/` and `projects/factory-cluster/` on top
of the root `REVIEW.md`. The edge, Authentik and the admission policies are
the factory cluster's trust boundary.

- **Every route signs in.** Each HTTPRoute attaches to the edge gateway, under
  its gateway-wide Authentik OIDC policy. The `oidc-sentinel` route stays.
  Authentik's admin and setup paths keep answering 403 publicly.
- **Hostnames are owned.** A new route's namespace gets a hostname rule in
  `edge-admission`, and the cfgate tunnel names the hostname.
- **OIDC redirect URIs are exact**: no wildcards, and none left on a hostname
  that moved.
- **Public routes** serve only mock or fixture data cached by the CDN, with
  no secrets and no calls into the cluster.
- **Network policy.** A new workload declares its ingress and egress; the
  cluster is default-deny.
- **Private images.** A namespace running a private `ghcr.io/jomcgi` image
  has the GHCR pull secret: nodes hold no ghcr credentials and Spegel does not
  mirror ghcr.io (#7894, #7914).
- **Node changes.** A change under `factory-cluster/nodes/` must take effect
  on a rerun of `bootstrap-nodes.sh`; k3s reads its config only at start
  (#7894).
