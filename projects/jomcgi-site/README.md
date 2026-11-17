# jomcgi.dev site

The public jomcgi.dev site as static files on Cloudflare (HOME-174). An
assets-only Worker serves the pages, so no script runs for any request.
Serving costs nothing, a crawler cannot cause per-request charges, and no
public request reaches a cluster. Sections move here one path at a time.

```
web/ (Astryx pages) ──vite, pre-render──► dist ──┐
wrangler/ (pinned CLI) ──────────────────────────┤
data/ (R2 snapshot) ─────────────────────────────┤
wrangler.jsonc, run.sh ──────────────────────────┴─► image
image ─► run.sh (Sync hook Job, and refresh CronJob every 30 min):
           copy site to /tmp ─► snapshot R2 into dist/data ─► wrangler deploy --env <stage>
  dev  → Worker jomcgi-site-dev on next.jomcgi.dev
  prod → Worker jomcgi-site, then the edge Job (PostSync):
         edge/routes.json + edge/cache-rules.json ─► zone jomcgi.dev
```

## Layout

| Path             | What                                                                                                                          |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `web/`           | The Astryx app: one HTML entry per page, the nav and section list in `src/sections.ts`, hosting headers in `public/_headers`. |
| `wrangler.jsonc` | The Worker, its assets and the dev host. Prod lists no routes.                                                                |
| `data/`          | The data snapshot: `sources.json` and the tool that copies them into the site before each deploy (HOME-209).                  |
| `edge/`          | The prod Worker's routes and the zone's cache rules, and `apply`, which makes Cloudflare match them (HOME-192).               |
| `wrangler/`      | The pinned `wrangler` CLI, packaged as image layers by rules_js.                                                              |
| `apko.yaml`      | The deploy image: bash and busybox for the rules_js launcher, CA roots, and Node's C++ runtime.                               |
| `chart/`         | The deploy Job (factory Helm library), its 1Password items and its only egress, `api.cloudflare.com:443`.                     |
| `deploy/`        | The dev stage's Argo CD Application.                                                                                          |

## Contracts

- **URLs.** A section has one top-level prefix (`/hikes`). It stays linked to
  its current jomcgi.dev page until it goes live here. Setting `live: true` in
  `src/sections.ts` and adding its patterns to `edge/routes.json` moves it.
  Removing them rolls it back: the edge Job deletes each route of Worker
  `jomcgi-site` that is not in the file, and the path falls back to the hub.
  `wrangler` cannot do this: with a zone-scoped token it adds routes but never
  deletes them, so the prod env lists none. Cloudflare matches a route against the
  whole URL, query string included, so a pattern ends in `*` (`jomcgi.dev/cv*`
  also serves `/cv?ref=x`). An exact pattern is allowed only with a reason in
  `exactBecause`, and a test keeps every pattern off paths the hub or other
  services still own.
- **Rollback.** `edge.serveRoutes` in the prod Application turns every route
  on or off. Off, the edge Job deletes all routes of `jomcgi-site`, so every
  jomcgi.dev path goes back to the hub, and the cache rules still apply. The
  prod Application is registered with it off, and a later PR turns it on, so
  reverting that PR is the rollback; it needs no chart publish. Remove the
  Application only after its routes are gone, because removing it does not
  delete them. Prod pins below the first chart with this switch refuse or
  ignore it, so do not lower the prod pin below that version.
- **Zone rules.** `edge/cache-rules.json` is the whole cache-settings
  entrypoint. A live rule that is not in the file blocks the cache-rules
  write, so a dashboard rule is never lost; import it by PR. Routes and cache
  rules are independent halves: a blocked half changes nothing, the other half
  still applies, and then the edge Job fails and names what it skipped. Only
  the prod stage runs the edge Job (`edge.enabled`), because zone config is
  not per stage.
- **Pre-render.** `build.mjs` runs the Vite build, then renders each page
  with React at build time into its HTML entry, in place of `<!--ssr-->`.
  A page reads fully, with its preview tags, before any script runs, and the
  browser hydrates the same tree (`src/root.tsx`). `:prerendered_test` fails a
  built page whose root is empty, and a unit test fails a page whose
  hydration does not match its HTML. Each entry imports only its own page.
- **Data.** A section that shows changing data reads
  `/data/<section>/latest.json` on the site's own origin, then the
  content-named files it lists (`/data/<section>/v/<name>-<sha256:12>.json`).
  `data/sources.json` names each section's source objects in R2.
  `run.sh` copies the site to `/tmp`, runs the snapshot (`data/`) to fill
  `web/dist/data`, then deploys the copy. The Sync-hook Job runs it on every
  sync and the `jomcgi-site-refresh` CronJob every 30 minutes, so data is at
  most 30 minutes behind R2. Same-origin static assets need no CORS and cost
  nothing per request. HOME-178 planned a public bucket on `data.jomcgi.dev`;
  until it exists, this is the data path, and moving to it changes only the
  base path pages read.
- **Caching.** `/assets/*` files are named by content hash and cached forever.
  A deploy serves new files at once, so routine releases need no cache purge.
- **Performance.** Each page's initial JavaScript must stay under 150 KiB
  gzipped. `budget.mjs` measures it, `:budget_test` runs it on the build, and
  a unit test proves it fails an over-budget page.
- **Accessibility.** Pages have no axe violations in happy-dom, and the colour
  tokens in `src/palette.css` meet WCAG contrast in light and dark modes.
  Colour contrast on real CSS is checked in a browser after deploy (HOME-174).

## Build and test

From `web/`: `pnpm build`, `pnpm test`, `pnpm typecheck`, `pnpm budget`.
Bazel runs the same checks: `bb remote test //projects/jomcgi-site/...`.

## Deploy

CI publishes the image and chart on merge. A sync of the stage's Application
runs the deploy Job as an Argo CD Sync hook. Argo CD does not diff hooks, so
the `jomcgi-site-release` ConfigMap carries the chart version, the image
digest, the stage and the edge switches (`edge`, `serveRoutes`). Each new
chart or image, a change to a Job alone, and a change to `edge.serveRoutes`
alone make the Application OutOfSync and sync it again. A failed deploy is a
failed sync.

The dev Application is listed in
`projects/platform-factory/kustomization.yaml`. Its Job reads 1Password item
`vaults/k8s-homelab/items/jomcgi-site-cloudflare` (fields `api-token` and
`account-id`; permissions are listed in HOME-178). The token allows calls
from the home network only, and pods reach Cloudflare over IPv4. Kargo
takes over the stage pins when HOME-116 slice 2 lands.

`public/robots.txt` lets crawlers fetch every page, so they can read each
page's indexing rules. `public/_headers` sends `X-Robots-Tag: noindex` for
the preview host only.
