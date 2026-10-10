# Public Tier Preflight Checklist

Run through this before merging any change that serves data on the PUBLIC tier (the `jomcgi.dev` apex, served by `monolith-public`). Every item below has caused its own follow-up fix PR when skipped, so treat it as a gate, not a suggestion.

## 1. public_reader grants

Every table the public tier reads needs an explicit `GRANT SELECT ... TO public_reader`. A new table added to an already-granted schema is **not** automatically covered unless that schema's original grant migration used `GRANT SELECT ON ALL TABLES IN SCHEMA ...` plus `ALTER DEFAULT PRIVILEGES`. Check the schema's grant migration before assuming a new table inherits access.

Missing this is invisible in review and in tests: it only shows up as a 503 at runtime, on a real curl against the public URL.

A PreToolUse hook (`bazel/tools/hooks/check-public-reader-grant.sh`) blocks new public-schema `CREATE TABLE` migrations that lack a grant. It only catches the table-creation case, its schema list is hand-maintained, and it fires only for edits made through Claude Code, so still check by hand.

Reads are the only thing `public_reader` covers. The one write path on the public tier is `public_writer`, scoped to DML on `chat_public` (`projects/monolith-public/chart/values.yaml`, `publicWriter:`). Do not widen it for a new feature; add a proxy route on the private tier instead.

## 2. No `/api` on the public origin

The public tier deliberately has no `/api` ingress rule (`projects/monolith-public/chart/templates/httproute-public.yaml`). Public pages must fetch data via a same-origin SvelteKit `+server.js` proxy route, never a client-side fetch to `/api/...`. If a public page calls `/api/...` directly, it will work against the private/monolith origin in dev and fail (or worse, silently hit nothing) on the actual public origin.

`/functions/` is the one prefix that reaches the public backend from the internet, rate-limited at the route. Anything new that needs the backend goes through the frontend proxy, never a second rule on that route.

## 3. gazelle-exclude vs the public binary

If a package directory is gazelle-excluded, the public binary's BUILD glob may deliberately exclude its sources from the public image. Importing that package from public-served code raises `ModuleNotFoundError`, but only in the public image, not locally and not in the main monolith image.

`main_public_imports_test` catches this in CI, but when adding a new import to public-served code, check the relevant BUILD glob yourself first rather than waiting for CI to tell you.

## 4. is_global / visibility filtering

Public reads must filter to the public corpus, for example `is_global = true`. A schema-wide grant without row-level filtering does not just fail to serve data correctly, it leaks private rows to the public tier. Granting a table is necessary but not sufficient: the query itself has to filter.

## 5. Explicit shared-cache contract

For every new public data route, follow the
[public response cache contract](#public-response-cache-contract) below.
Confirm that the response is anonymous and cookie-free, contains no
caller-specific data, and does not depend on authentication, personalization,
or session state. Authenticated or personalized responses must not use the
shared-cache contract.

Set explicit `Cache-Control` and `Cloudflare-CDN-Cache-Control` response headers
at the same-origin SvelteKit proxy with `cloudflareCacheHeaders()` and a
route-specific constant from `projects/monolith/frontend/src/lib/cache-headers.js`.
Keep a backend mirror synchronized when one exists. If the browser polls the
route, start with the documented five-minute cadence and justify route-specific
exceptions against the data refresh rate and edge lifetime. Check both browser
and edge directives rather than treating them as one TTL.

Header tests prove only the landed code contract. Keep live cache acceptance
unverified until a post-deploy repeated request under the
`jomcgi.dev` apex hostname rule shows an appropriate `cf-cache-status` and
`age` response. Do not claim an edge hit from CI or from header presence alone.

## Public response cache contract

The shared Cloudflare cache contract applies only to public, anonymous,
cookie-free responses covered by the `jomcgi.dev` apex hostname Cache Rule.
A cacheable response must not depend on `Authorization`, a session, cookies, or
any other caller-specific state, must not contain personalized data, and must
not set a cookie. Authenticated or personalized responses are outside this
contract and must not be marked `public` for shared caching.

Every new public data route must set its cache policy explicitly at the
internet-facing response. For a SvelteKit same-origin proxy, select the
route-specific constant from
[`cache-headers.js`](../../projects/monolith/frontend/src/lib/cache-headers.js)
and pass it through `cloudflareCacheHeaders()`. Do not rely on a backend header
surviving the proxy, on a Cloudflare default TTL, or on the hostname rule to
invent a policy. Keep a mirrored backend policy synchronized when the backend
also emits the header.

The stats proxy is the canonical short-lived data example. Its policy is:

```http
Cache-Control: public, max-age=0, s-maxage=60, stale-while-revalidate=86400, stale-if-error=31536000
Cloudflare-CDN-Cache-Control: public, max-age=60, stale-while-revalidate=86400, stale-if-error=31536000
```

The two headers deliberately address different caches. `max-age=0` in
`Cache-Control` makes a browser revalidate on each use instead of retaining a
stale JSON snapshot. `s-maxage=60` gives shared caches a 60-second lifetime.
`cloudflareCacheHeaders()` converts that shared lifetime to `max-age=60` in the
higher-precedence Cloudflare-only header because Cloudflare treats `s-maxage`
as `proxy-revalidate`, which would disable the stale directives. See the
canonical use in the
[`/app/notes/stats` proxy](../../projects/monolith/frontend/src/routes/public/app/notes/stats/+server.js)
and its mirrored backend value in
[`home/observability/router.py`](../../projects/monolith/home/observability/router.py).

Default a new public data poller to the existing five-minute cadence in
[`homepage-stats.js`](../../projects/monolith/frontend/src/routes/public/homepage-stats.js),
then choose a different interval only when the data's refresh rate and user
experience justify it. The Notes app's 20-second live readout is an existing
route-specific example: browser revalidation occurs on every poll, while the
60-second shared lifetime lets the edge absorb those requests. Browser and edge
freshness are separate decisions, so never use browser caching as a substitute
for a polling interval.

These headers establish the code-side contract, not proof that the live edge
accepted the response. After deployment, verify repeated requests on the
hostname covered by the rule and inspect `cf-cache-status` and `age`. Keep a
route's live cache acceptance recorded as unverified until that check succeeds.
Section 5 above and the rollout checks below carry the implementation and
rollout checks.

## Rollout: the public origin is `monolith-public`

`jomcgi.dev` is served by the `monolith-public` chart, so a change that only moves the `monolith` chart does not move the public origin. Chart versions are written back on `main` after merge (ADR platform/009): a PR never touches `Chart.yaml` `version:` or `targetRevision:`. On the hub Kargo promotes `monolith-public` from that published version, so the git pin under `projects/gke-apps/monolith-public/` is a floor, not the deployed version. Confirm the `chart-version-bot` write-back landed, then read the live value before curling the route:

```bash
kubectl get application monolith-public -n argocd -o jsonpath='{.spec.sources[0].targetRevision}'
```

## Post-deploy verification

Don't consider a public route done until you've curled it live:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://jomcgi.dev/<new-route>
```

A 200 from a live curl of the actual public URL is the only verification that counts here. Passing tests and a green CI run do not confirm the public_reader grant, the proxy route, or the chart bump actually landed together in prod.
