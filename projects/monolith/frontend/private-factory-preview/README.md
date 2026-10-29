# Private factory fixture QA

This harness mounts the three production private factory pages inside their
real root and private layouts. It includes the root error node's preloaded
legacy stylesheet, dashboard tokens and the shared sheet. All data and
operator responses are invented. Browser fetch is an explicit in-memory
allowlist, CSP denies connections, and Chromium rejects non-fixture requests.
No private service, account or credential is used.

The public `factory-preview` remains separate: its module guard excludes private
code. The private build permits only these three routes and the existing pure
execution helpers they consume. Its `build.json` records the source digests and
tested SHA; it rejects server modules. Neither fixture enters the production
application build.

The `Factory mobile preview` workflow invokes the public browser entry point,
which runs `ci_check.py` after its unchanged public checks. This runs the existing
per-route behavior tests, the tier-layout tests, builds these components and
invokes Chromium on Linux. No workflow or permission changes are needed. The
layout tests also run as `:private_factory_fixture_test`; route
behavior suites remain in the existing frontend `:test` target.

From `projects/monolith/frontend`, on a clean tested head:

```sh
pnpm exec vitest run --config private-factory-preview/vitest.config.js
FACTORY_PREVIEW_SHA=$(git rev-parse HEAD) pnpm exec vite build --config private-factory-preview/vite.config.js
python private-factory-preview/browser_check.py --artifact private-factory-fixture-preview --output /tmp/private-factory-evidence --expected-sha $(git rev-parse HEAD)
```

Use the workflow's pinned Playwright and Chromium. For local development set
`PRIVATE_FACTORY_PREVIEW_OUT` to a temporary directory and do not claim a dirty
build as exact-commit evidence.

The existing `factory-preview-evidence-<sha>` upload contains `private/MATRIX.md`,
which links every private full-page screenshot. `private/results.json` records the head,
source/output SHA-256 digests, route, scenario, viewport, scheme, page/container
overflow, body scroll contract, sticky navigation, control styles and sizes,
action requests and failures. The base matrix is all three routes at 390 and
1440 in light/dark. Additional cases cover empty/unavailable/missing views,
expanded tasks/events, relationship forms and the inline close confirmation.
The existing relationship remove control sends immediately; this migration
retains that behavior. The escalation close remains a two-step confirmation.

Browser actions prove confirmation pinning across poll reordering, request-key
replay versus refusal, busy/error feedback, query-state toggling, relationship
add/remove and persisted scheme selection against the opposite system scheme.
Screenshots require visual review. CI evidence is linked from the PR, expires
after 14 days and must be regenerated if the PR head changes.

## Non-factory task integration (#7400)

The board fixtures include manual/external tracking rows alongside factory
delivery. The tracking detail scenario renders the production tracking branch:
explicit source and owner, tracking-only status, completion evidence and no
factory receipt, admission, execution progress, relationship or decision
controls. Recording mutations remain MCP-only. Missing owner/source data reads
as unknown, not as factory ownership. Long source/owner text is included in the
mobile/desktop, light/dark and enlarged-text checks.

The continuation adds 200% computed-text captures at both widths and schemes.
Decision tests cover replacement on the same receipt, duplicate taps, accepted
but stale lists and immutable request replay after remount. Session storage keeps
request identity within the tab; it does not grant authorization or bypass the
server's decision and idempotency checks.
