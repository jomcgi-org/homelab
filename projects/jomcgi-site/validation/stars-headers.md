# Stars CSP under Cloudflare's asset handler (HOME-210)

`web/public/_headers` gives `/stars` its own Content-Security-Policy. The `/*`
rule sets the site policy, and the `/stars` rule after it removes that policy
(`! Content-Security-Policy`) and sets one that adds the base map. This record
shows what Cloudflare's asset handler sends for that file.

**Method (2026-11-18).** `wrangler dev` 4.162.0 (the deploy Job's pinned
version) ran locally on workerd, with the `_headers` and `_redirects` of
#8181 at 12bb6cc81 and one stub HTML file per page. The config had the same
`assets` settings as `wrangler.jsonc`: `html_handling: auto-trailing-slash`
and `not_found_handling: 404-page`. `curl -D -` read each response.

| Path | Status | CSP headers | OpenFreeMap and `worker-src 'self' blob:` |
| --- | --- | --- | --- |
| `/stars` | 200 | 1 | yes |
| `/stars?mode=historical` | 200 | 1 | yes |
| `/stars/`, `/stars.html` | 307 to `/stars` | 1 | no (site policy) |
| `/` | 200 | 1 | no (site policy) |
| `/docs` | 200 | 1 | no (site policy) |
| `/llm-leaderboard` | 200 | 1 | no (site policy) |
| `/app/stars` | 301 to `/stars` | 1 | no (site policy) |

So `/stars` gets exactly one policy, the map policy, and the other pages keep
the site policy.

**Not shown here.** The real base map tiles from `tiles.openfreemap.org` need
the deployed page on `next.jomcgi.dev` after the dev deploy; that live check is
HOME-210 criterion 15. `web:stars_browser_test` checks the map under the
`/stars` policy with a local style fixture.
