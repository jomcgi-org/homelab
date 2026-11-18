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
`CONNECTOR_STATUS_URL` without credentials or visitor headers (two-second
timeout, generic 503). The page refreshes every 30 seconds; tool links work if
status fails. CI publishes the image and chart and pins the chart version.
The chart owns the connector status Service and its Cilium grant: only the
landing pods reach port 8082, and that is their only application egress. Its
render test checks both policies and the unchanged private route.

Validate with `ci`; PR CI covers factory guests without the local toolchain.
Live acceptance after rollout: an anonymous `/` redirects to Authentik;
sign-in shows HOME-20 to HOME-23 (assigned to member `joe`, no delegate) under
Needs you, live work under In flight, recent merges under Shipped recently, and
the five tool cards; the ArgoCD link signs in at its hostname. See
[the factory architecture](../factory/ARCHITECTURE.md#factory-front-page).
Keep the Authentik admin/setup 403/404 probes and the edge's
`/oauth2-sentinel` 404 unchanged.

The CSP allows `connect-src 'self'` and keeps `style-src 'self'`. The app uses
the neutral theme's `/built` export and imports its `theme.css` into the Vite stylesheet. In
[Astryx 0.7.11's Theme](https://github.com/facebook/astryx/blob/v0.7.11/packages/core/src/theme/Theme.tsx),
`useThemeStyleInjection` returns immediately for `__built` themes. The
`csp.test.tsx` regression test checks that rendering this page creates no
inline style elements or attributes, so no inline hashes or
`'unsafe-inline'` exception is needed.

## Transferring connector status ownership (HOME-363)

Before either chart pin advances, protect the two live objects from automated
pruning during independent Application syncs. Run on the factory cluster:

```sh
kubectl -n factory annotate --overwrite service/factory-connector-status \
  ciliumnetworkpolicy/factory-landing-status argocd.argoproj.io/sync-options=Prune=false
```

Pause auto-sync on factory-landing through its Git-managed Application and
wait for any running sync to finish before applying the annotation. Sync
software-factory at the chart version that renders both objects. Verify both
live objects have the software-factory tracking ID and the Prune=false
annotation. Then advance the landing pin to the chart without these objects,
restore its automated sync, and verify the signed-in page can load /api/status.

For rollback, pause software-factory auto-sync first. Restore and sync the
previous landing chart that renders both objects. Verify factory-landing owns
both live objects before restoring the previous factory pin and resuming its
sync. If the factory pin is lowered first, the live Prune=false annotations
keep the Service and ingress grant available while the landing chart resumes
ownership. Check /api/status again after both Applications reconcile.
