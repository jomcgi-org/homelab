# Factory storyboard snapshots

The public, read-only storyboard will tell the factory's story from goals
through individual agent sessions. This package defines its snapshot contract
and invented fixtures. No real sessions were used. It deploys nothing.

HOME-68 is split into two reviewable changes: this contract and fixture slice,
then the Astryx site on `factory/HOME-68-site`, based on `factory/HOME-68`.
The second slice adds `web/`, all four pages, the snapshot reader and browser
tests. Review its diff against this slice until this slice lands on `main`.

## Snapshot contract

`schema/v1/snapshot.schema.json` is a draft-07 JSON Schema. Its definitions
describe the manifest and the story, goal, issue and session objects.
`schema/types.ts` carries the corresponding TypeScript types. Every document
has `version: 1`; object documents also have a `kind`. A new incompatible
contract gets a new schema directory and version. Readers must reject
unsupported versions rather than treating them as the current format.

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
The reader must fill them from the story's stats and display an en dash for an
unknown key. All text, including tool input, tool output and diffs, is plain
text. Readers must never interpret it as HTML or Markdown. Only typed IDs
and PR numbers become links; PR links use
`https://github.com/jomcgi-org/homelab/pull/<number>`.

A withheld turn contains only its number, timestamp, role and `withheld: true`.
It cannot contain text, tool calls or a diff. Readers must display:
"This turn was withheld because it matched the secret scan."
The publisher owns scanning and withholding; the site performs no secret scan.

## Fixtures and validation

The fixtures describe a fictional reading-list project. They include every
object kind and state, an empty goal, an unknown stat placeholder, a withheld
turn and a long transcript with tool calls, diffs and script-looking plain text.
`schema_test.py` uses the repository's existing `jsonschema` dependency to
check the schema, every fixture, exact byte hashes, typed cross-links and
invalid inputs. It runs independently of the later web package:

```sh
python3 -m pytest projects/factory-storyboard/schema_test.py
```

The Bazel targets are `//projects/factory-storyboard:schema_test` and
`//projects/factory-storyboard:rehash_test`. The rehash regression tests also
run with `node --test projects/factory-storyboard/fixtures/rehash.test.mjs`
from the repository root. Run `ci lint`,
`ci regen` and `ci ratchet` before pushing. BUILD generation and remote tests
run in PR CI for factory source guests.

After editing fixture objects, run this from the package root:

```sh
node fixtures/rehash.mjs
```

It reads all objects before changing files, rejects occupied or duplicate
destinations, then creates the new objects and updates the manifest paths
before removing the old files. A conflict found before writing leaves every
file unchanged; resolve it before rerunning. New objects use exclusive creation
so existing files cannot be overwritten.
The script writes the manifest to a temporary file before replacing the original
by rename. Failed writes or renames preserve the original manifest and objects.
The rehash tests inject both failures and check that edited inputs remain intact.
The repository formatter excludes immutable fixture objects to preserve
their addresses.

## Later slices

The site will fetch `/data/manifest.json`, then `/data/<object path>`, and
poll the manifest every 60 seconds. Missing or invalid objects must leave
the rest of the page readable. Vite will serve fixtures at those paths in
development and tests; the production build will contain no fixture data.

The factory-cluster publisher will write sanitized objects and then the
manifest to Garage. A mirror will copy them to Cloudflare R2. A narrative
agent will write plain-text narrative fields using stat placeholders. These
writers share this contract; the monolith's publication payloads do not carry
over.

The Worker will serve the built site and `/data/*` from R2, with `index.html`
as the fallback for client-side routes. It must serve JSON 404s for missing
data instead of that HTML fallback, allow the manifest to refresh, and cache
immutable objects by their content address. It will send the site's CSP as
an HTTP header, including `style-src 'self'` and `frame-ancestors 'none'`.
There is no image, chart, route or Worker deployment in this package.
