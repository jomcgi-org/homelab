# Factory landing

A links-only Astryx page at `factory.jomcgi.dev`. The factory edge performs
Authentik sign-in before serving it. ArgoCD lives at
`factory-argocd.jomcgi.dev` and keeps its own OIDC sign-in behind the edge.

The separate Vite entry in `projects/monolith/web/grimoire/factory/` shares
Grimoire's pinned React/Astryx 0.7.11 toolchain and compiled sheet theme.
It imports no Grimoire API or application state. From that package:

```sh
pnpm exec vite build --config factory/vite.config.ts
pnpm typecheck
pnpm test
```

Bazel builds `//projects/monolith/web/grimoire:factory_build` and layers the
static output into `//projects/factory-landing:image`. The Node HTTP server
has no API, credentials or application egress. CI publishes the image and
Helm chart and writes the chart version into `deploy/application.yaml`.
The chart's render regression test checks the route and runtime boundary.

Validate with `ci`; PR CI covers factory guests without the local toolchain.
Live acceptance after rollout: an anonymous `/` redirects to Authentik;
sign-in opens the five cards; the ArgoCD link signs in at its new hostname.
Keep the Authentik admin/setup 403/404 probes and the edge's
`/oauth2-sentinel` 404 unchanged.
