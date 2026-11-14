# Public response cache boundary

The packaged HTTP server applies `no-store` to both `Cache-Control` and
`Cloudflare-CDN-Cache-Control` before writing response headers. It covers
cookie and authentication headers, responses that set cookies, private and
friends routes and hosts, the Grimoire chat subtree, non-read methods, errors,
and responses that vary by identity. A restrictive policy in any cache
header wins over a conflicting public policy. The server hook applies the
same policy after SvelteKit resolves a response and removes ETags before
SvelteKit can answer a conditional request with 304.

SvelteKit emits normalization redirects and some errors before its server
hook runs. Adapter-node also serves static files before SvelteKit. The
`cache-server.js` entry wraps final HTTP headers for these paths while keeping
adapter-node's startup, graceful shutdown and socket activation. Both image
builds package this entry and use it as their command.

Public app and Scotland routes need an explicit anonymous cache policy in
their loader or proxy. Missing policies stay `no-store`. Anonymous responses
with existing explicit policies retain their current lifetimes. The hook
does not add shared caching to other public pages.

`src/public-cache.test.js` discovers the public app and Scotland route files
and tests each against cookie and authorization requests. It also exercises
the real primary app loaders through the response hook. The packaged HTTP
regression in `src/public-http-cache.test.js` launches the deployed entry and
checks trailing-slash redirects, early errors and static assets. The existing
`//projects/monolith/frontend:test` target includes these tests through its
`src/**/*.test.js` glob.

## Edge acceptance

On 2026-11-14, a fresh URL for `/app/hikes` requested with a synthetic `gcs`
cookie returned `cf-cache-status: MISS` and a public cache policy. The origin
guard changes that response to `no-store`. It cannot run when Cloudflare
serves a previously cached response without reaching the origin.

The hostname Cache Rule lives in the Cloudflare dashboard. Incoming cookie
and authentication bypass, including already stored entries, requires the
account-level check in [#7961](https://github.com/jomcgi-org/homelab/issues/7961).
No dashboard setting or cache purge is changed by this code. Production
acceptance remains open until the rollout and that edge check are verified.

## Freshness

This change preserves the current numerical cache policies. Desired maximum
staleness for positions, forecasts and published static snapshots is tracked
in [#7960](https://github.com/jomcgi-org/homelab/issues/7960).

Public Notes graph snapshots have an existing stricter contract. The process
cache consumes the origin's age and caps its lease at 60 seconds and the
earliest review deadline. Each response advertises only the remaining lease;
process and edge residence do not each get a new 60 seconds. Those snapshots
use `must-revalidate` and never permit stale fallback.
