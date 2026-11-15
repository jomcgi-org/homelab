# Factory storyboard

A public, read-only account of the factory's goals and the work behind each
change. This slice runs on invented fixtures and deploys nothing.

The site is the second reviewable part of HOME-68, on `factory/HOME-68-site`.
Its base is `factory/HOME-68`, which adds the snapshot contract, fixtures and
independent schema tests. Review this branch against that base until it lands
on `main`; the combined change exceeds the factory review's line budget.

From the repository root, install the pinned workspace dependencies with
`pnpm install --frozen-lockfile`. Then, from `web/`:

```sh
pnpm dev
pnpm typecheck
pnpm test
pnpm build
```

The development server serves the fixtures at `/data/manifest.json` and
`/data/objects/<sha256>.json`. Open `/`, `/goals/reading-list`,
`/issues/DEMO-104` and `/sessions/demo-build`. The empty goal is at
`/goals/quiet-shelf`. All identifiers, PR numbers, records and measurements
are fictional. No real sessions were used.

The package copies factory-landing's Vite/React and Bazel setup, including
Astryx core and the built neutral theme pinned to 0.7.11. It has no added
runtime dependencies. The workspace's dependency admission policy still
applies. Bazel targets are:

```text
//projects/factory-storyboard/web:build
//projects/factory-storyboard/web:build_test
//projects/factory-storyboard/web:test
//projects/factory-storyboard/web:typecheck
//projects/factory-storyboard:schema_test
```

Run `ci lint`, `ci regen` and `ci ratchet` before pushing. BUILD generation
and remote tests run in PR CI for factory source guests.

## Snapshot contract

`schema/v1/snapshot.schema.json` is a draft-07 JSON Schema. Its definitions
describe the manifest and the story, goal, issue and session objects.
`schema/types.ts` carries the corresponding TypeScript types. Every document
has `version: 1`; object documents also have a `kind`. A new incompatible
contract gets a new schema directory and version. Readers reject unsupported
versions rather than interpreting their fields as the current format.

`manifest.json` is the only mutable document. It holds `generatedAt`, the
story path, and maps of every current goal ID, issue key and session ID to
an object path. Object paths have the form `objects/<sha256>.json`, where
the digest covers the exact UTF-8 bytes, including whitespace and a trailing
newline. Reformatting an object changes its address. Objects are immutable;
publish all new objects before replacing the manifest. References use typed
IDs, so a goal or issue can refer to another record without embedding its
current content hash.

The story carries its headline, narrative, goal chapters and progress,
named stats, chart series, work in progress and recently shipped issues.
Goals carry their chapter, issue keys and burn-up measurements. Issues carry
one ordered timeline with issue creation, session turns, PRs, review rounds,
checks, merge queue changes and merge. Sessions carry individual turns, tool
calls and diffs. The allowed states are Working, In review, In merge queue,
Shipped and Stopped.

Narrative fields allow numeric claims only through `{{stat:key}}` placeholders.
The reader fills them from the story's stats. Unknown keys display an en dash.
Other text fields, including tool input, tool output and diffs, are plain text.
The site never interprets snapshot text as HTML or Markdown. Only typed IDs
and PR numbers become links; PR links always use
`https://github.com/jomcgi-org/homelab/pull/<number>`.

A withheld turn contains only its number, timestamp, role and `withheld: true`.
It cannot contain text, tool calls or a diff. The reader displays:
"This turn was withheld because it matched the secret scan."
The publisher owns scanning and withholding; this site performs no secret scan.

`schema/validate.ts` interprets the subset of draft-07 keywords used by the
schema, with no dynamic code generation or added dependencies. Unknown
keywords fail closed. Extend the interpreter and tests when adding schema
keywords. Runtime checks reject extra fields, validate every object and
verify its hash and manifest identity before exposing it to a page.

## Reading snapshots

The app fetches `/data/manifest.json`, loads the listed objects and polls the
manifest every 60 seconds. Validated immutable objects are reused while the
manifest still references them. Missing, invalid and wrongly addressed objects
remain unavailable independently and are retried on the next poll. A failed
manifest refresh keeps the last available snapshot visible with a notice.
Requests time out after 15 seconds and are aborted when the app unmounts.

Routes are handled in the browser. Native anchors retain keyboard, new-tab
and copy-link behavior. Navigation moves focus to the page or transcript turn;
tools use native disclosure controls. SVG charts include tables. The layout
wraps prose, tables and diffs inside 16px phone gutters. The scheme switch
supports light and dark and remembers the visitor's choice when storage is
available.

The production build emits a CSP meta policy with `style-src 'self'` and
`script-src 'self'`. Astryx uses the `/built` theme export and external CSS.
`csp.test.tsx` checks all four routes, open disclosures and both themes for
inline styles. The development server uses Vite's normal CSS/HMR transport;
the build adds the CSP after Vite has extracted the stylesheet. The build
contains no fixture data.

After changing or formatting fixture objects, run this from the package root:

```sh
node fixtures/rehash.mjs
```

It renames objects to match their bytes and updates the manifest paths. The
repository formatter excludes immutable fixture objects to preserve those
addresses. Tests
check every fixture against the schema, its hash and typed cross-links, as
well as loading failures, polling, text rendering, navigation and CSP.
The independent Python test checks the contract using the repository's
existing `jsonschema` dependency. Run it from the repository root with
`python3 -m pytest projects/factory-storyboard/schema_test.py`; the browser
tests also exercise the runtime schema interpreter.

## Later slices

The factory-cluster publisher will write sanitized objects and then the
manifest to Garage. A mirror will copy them to Cloudflare R2. A narrative
agent will write the schema's plain-text narrative fields using stat
placeholders. These writers share this contract; the monolith's existing
publication payloads do not carry over.

The Worker will serve the built site and `/data/*` from R2, with `index.html`
as the fallback for client-side routes. It must serve JSON 404s for missing
data instead of that HTML fallback, allow the manifest to refresh, and cache
immutable objects by their content address. It will also send the CSP as an
HTTP header, adding `frame-ancestors 'none'`, which a meta policy cannot
enforce. `connect-src 'self'` permits the snapshot reads. There is no image,
chart, route or Worker deployment in this package yet.
