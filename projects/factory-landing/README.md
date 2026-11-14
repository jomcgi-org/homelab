# Factory landing

A links-only Astryx page at `factory.jomcgi.dev`. The factory edge performs
Authentik sign-in before serving it. ArgoCD lives at
`factory-argocd.jomcgi.dev` and keeps its own OIDC sign-in behind the edge.

`web/` is an independent Vite/React package, with Astryx core and the neutral
theme pinned to 0.7.11. Its local stylesheet carries the sheet palette and
typography values. The app and image have no monolith build inputs. From
`web/`:

```sh
pnpm build
pnpm typecheck
pnpm test
```

Bazel builds `//projects/factory-landing/web:build` and layers the static
output into `//projects/factory-landing:image`. `apko.yaml` and its lock
own the nonroot Node runtime. The Node HTTP server has no API, credentials
or application egress. CI publishes the image and Helm chart and writes
the chart version into `deploy/application.yaml`.
The chart's render regression test checks the route and runtime boundary.

Validate with `ci`; PR CI covers factory guests without the local toolchain.
Live acceptance after rollout: an anonymous `/` redirects to Authentik;
sign-in opens the five cards; the ArgoCD link signs in at its new hostname.
Keep the Authentik admin/setup 403/404 probes and the edge's
`/oauth2-sentinel` 404 unchanged.
