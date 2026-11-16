# Factory landing

Joe's factory front page at `factory.jomcgi.dev`: tasks needing him, work
in flight, the ten latest merges, then tool links. The factory edge performs
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
own the nonroot Node runtime. `GET /api/status` proxies the connector's fixed
`CONNECTOR_STATUS_URL` without credentials or visitor headers, with a two-second
timeout and a generic 503 on failure. The page refreshes every 30 seconds and
keeps the tool links available if status fails. CI publishes the image and Helm
chart and writes the chart version into `deploy/application.yaml`.
The landing chart owns the connector status Service and its Cilium ingress
grant in the connector namespace. Only the landing pods may reach port 8082;
the landing pods' application egress is only that port (the cluster baseline
provides DNS). The connector's metrics port remains separate. The chart's
render regression test checks both policies and the unchanged private route.

Run the proxy tests from this directory with `node --test server.test.mjs`.
Connector checks are `//projects/factory/connector/{status,itsaplan,github,store}:all`;
the landing targets include `:server_test`, `web:{test,typecheck,build_test}`
and `chart:landing_test`.

Validate with `ci`; PR CI covers factory guests without the local toolchain.
Live acceptance after rollout: an anonymous `/` redirects to Authentik;
sign-in shows HOME-20 to HOME-23 under Needs you, current factory work under
In flight, recent merges under Shipped recently, and the five tool cards.
The ArgoCD link signs in at its hostname.
Before accepting the status sections, confirm that the coordinator's project
scaffold contains `username: joe` with `kind: member`, and that HOME-20 to
HOME-23 have that `userId` as assignee and a null delegate. The upstream API
contract is linked in [the factory architecture](../factory/ARCHITECTURE.md#factory-front-page).
Without that member, Needs you shows as unavailable and the other sections
still render.
Keep the Authentik admin/setup 403/404 probes and the edge's
`/oauth2-sentinel` 404 unchanged.

The CSP allows `connect-src 'self'` and keeps `style-src 'self'`. The app uses
the neutral theme's `/built` export and imports its `theme.css` into the Vite stylesheet. In
[Astryx 0.7.11's Theme](https://github.com/facebook/astryx/blob/v0.7.11/packages/core/src/theme/Theme.tsx),
`useThemeStyleInjection` returns immediately for `__built` themes. The
`csp.test.tsx` regression test checks that rendering this page creates no
inline style elements or attributes, so no inline hashes or
`'unsafe-inline'` exception is needed.
