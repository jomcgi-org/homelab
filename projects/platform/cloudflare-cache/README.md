# Cloudflare public cache policy

`policy.json` is the reviewed desired state for the cache rule scoped to
`public.jomcgi.dev`, its supporting zone settings, and the sustained cache-miss
guardrail. The reconciler will not change zone-wide settings unless the live
cache rule has the exact public-only hostname expression and safe action
parameters. Private and authenticated hostnames are outside that expression.

`cloudflare_cache.py` is ready to use the existing `CLOUDFLARE_API_TOKEN`
repository secret. It does two independent things:

- Reconciles Browser Cache TTL to `0` (Cloudflare's API value for Respect
  Existing Headers), enables Always Online, and enables Smart Tiered Cache.
- Queries Cloudflare's supported `httpRequestsAdaptiveGroups` GraphQL dataset
  for `cacheStatus: "miss"` on `public.jomcgi.dev`. A rate above 5 percent over
  the complete 15-minute window opens one GitHub issue. A recovered window
  comments on and closes that issue. Fewer than 20 requests is insufficient
  data and never clears an existing alert.

The two-minute query lag avoids treating analytics ingestion delay as a quiet
tail. API failures fail the command instead of being interpreted as healthy.

## Operations

No scheduler currently invokes these commands. Adding the intended GitHub
Actions schedule remains operational work. Until that is resolved, settings
are not reconciled and the MISS threshold is not evaluated automatically. For
read-only local drift inspection:

```sh
CLOUDFLARE_API_TOKEN=... \
  python projects/platform/cloudflare-cache/cloudflare_cache.py check
```

After a change to either the Cloudflare policy or origin response headers,
verify the live public hostname with the three-request procedure in
[`docs/runbooks/public-tier-checklist.md`](../../../docs/runbooks/public-tier-checklist.md).
Repository tests and successful reconciliation do not establish a CDN hit.
Keep live acceptance unverified until the repeated requests show the expected
`cf-cache-status`, `age`, and unchanged origin cache directives.
