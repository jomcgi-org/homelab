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

## Rollout: the public origin is `monolith-public`

`jomcgi.dev` is served by the `monolith-public` chart, so a change that only moves the `monolith` chart does not move the public origin. Chart versions are written back on `main` after merge (ADR platform/009): a PR never touches `Chart.yaml` `version:` or `targetRevision:`. Confirm the `chart-version-bot` write-back for `monolith-public` landed before curling the route.

## Post-deploy verification

Don't consider a public route done until you've curled it live:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://jomcgi.dev/<new-route>
```

A 200 from a live curl of the actual public URL is the only verification that counts here. Passing tests and a green CI run do not confirm the public_reader grant, the proxy route, or the chart bump actually landed together in prod.
