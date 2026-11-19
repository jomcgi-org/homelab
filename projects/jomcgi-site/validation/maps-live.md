# Interactive maps on next.jomcgi.dev (HOME-383)

The stars and hikes maps load OpenFreeMap tiles only after a visitor selects
the map button. The site-check audits each page passively and does not select
it, and `web:stars_browser_test` uses a local style fixture. This record shows
the deployed maps with real tiles under the served CSP.

**Method (2026-11-19, 01:20Z).** `jomcgi-site-dev` ran chart 0.34.32 (main
commit 098aab134), synced at 01:16:06Z with its site-check passed. A pod in
`jomcgi-site-dev` on the factory cluster ran the site-check image's Chromium
(`launchBrowser` from `check/browser.mjs`) with the page CSP enforced
(`bypassCSP: false`). For each page it loaded the URL, selected the map button
(on `/hikes`, "Explore walks" and then "Show map"; on `/stars`, "Load
interactive map"), waited for network idle, and recorded every
`tiles.openfreemap.org` response, every CSP violation, page errors and the
`role=status` text.

| Page | Worker script | Worker `connect-src` has the tile host | `.pbf` vector tiles | PNG (relief, sprite) | `liberty` style, `planet` TileJSON, other JSON | Map canvas | Map error shown |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `/stars` | `/assets/maplibre-gl-worker-csp2-DdG60dM_.js` (200) | yes | 34 × 200 | 19 × 200 | 3 × 200 | 1104 × 585 | no |
| `/hikes` | `/assets/maplibre-gl-worker-csp2-DdG60dM_.js` (200) | yes | 34 × 200 | 19 × 200 | 3 × 200 | 1104 × 585 | no |

No OpenFreeMap request failed and no page error occurred. The only CSP
violation on each page was `script-src-elem` for
`https://static.cloudflareinsights.com/beacon.min.js`, the beacon that the edge
injects. The site-check ignores it, and HOME-382 ships the beacon from the site.

**Before the worker fix (chart 0.31.7, 2026-11-18).** The same check on
`/stars` got no vector tile. The worker came with the site policy
(`connect-src 'self' https://data.jomcgi.dev`), so each tile fetch in the
worker failed with `Failed to fetch (0)` and the map drew only the raster
relief. #8297 (HOME-420) gives the worker the map CSP and renames it to
`maplibre-gl-worker-csp2-*`, so browsers that cached the old worker fetch the
new one.
