# authentik

This deploys the official authentik Helm chart with a separately managed
CloudNativePG database, as decided by ADR 032.

Before ArgoCD can make the server ready, create the 1Password item at
`vaults/k8s-homelab/items/authentik`. It must contain a field named
`AUTHENTIK_SECRET_KEY` with a generated, durable value. The item is mirrored by
the 1Password Operator as `authentik-secrets` in the `authentik` namespace.

The database password is generated and managed by CloudNativePG through the
`authentik-pg-app` Secret. The authentik chart reads that password by reference,
so it is never committed to this repository.

The Gateway API route publishes the Authentik login and OIDC discovery endpoint
at `https://auth.jomcgi.dev`. It does not publish an Ember OIDC provider or
modify Ember's authentication configuration. Friend applications should use
separate HTTPRoutes with Envoy OIDC policies; do not attach those policies to
the Authentik route itself or the login flow will recurse.
