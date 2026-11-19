# Factory storyboard snapshots

The public, read-only storyboard will tell the factory's story from goals
through individual agent sessions. This package defines its snapshot contract
and invented fixtures. No real sessions were used for the fixtures.

HOME-68 added this contract and its fixtures. The reader lives at `/factory`
in `projects/jomcgi-site/web` (HOME-224). This package has no separate site.

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
The repository formatter excludes immutable fixture objects to preserve
their addresses.

## Reader and publication

The reader fetches `/data/factory/manifest.json`, then
`/data/factory/<object path>`, and polls the manifest every 60 seconds.
`schema/validate.ts` interprets the JSON Schema subset used by this contract
without generating code. The reader verifies hashes before displaying objects.
Missing or invalid objects leave the rest of the page readable. Vite serves
fixtures at those paths in development and tests; production builds contain none.

The factory-storyboard CronJob runs in the factory namespace every five
minutes. Its ServiceAccount uses the read-only connector database login and
a Garage write claim for factory-public. The publisher has no controller ingress grant.
The separate storyboard-task-reader verifies a publisher-specific bearer and
accepts GET requests for individually granted tasks. It uses its own upstream
identity and each owner's share. It returns only approved assistant-message
fingerprints confirmed in the completed task.

Provision two 1Password items before sync:

- `factory-storyboard-readers` holds the project-scoped read-only
  `ITSAPLAN_KEY`, a random `READER_TOKEN` of at least 32 characters and
  `APPROVED_HISTORY`. The latter is a JSON object keyed by issue key. Each
  entry can contain an approved literal title and description and a `tasks`
  array. Each task records its `session`, `task`, UTC completion time `at`,
  and the exact owner-reviewed assistant messages in `assistantTexts`.
  Keep these approved exports after connector cleanup. Follow the withdrawal
  procedure below when an owner removes an approval.
  An empty object publishes issue keys and metrics only.
- `storyboard-owner-shares` holds the reader's `UPSTREAM_BEARER` and
  `OWNER_GRANTS`, a JSON array. Each grant contains `namespace`, `agent`,
  `session`, `task`, the owner's `share`, and `approvedHashes` (SHA-256 of
  each approved assistant message's UTF-8 text). A session share grants
  read access. A separate explicit publication approval is required before
  adding any prose to either item.

After publisher rollout, run
`node projects/factory-storyboard/verify-rollout.mjs --mirror-only`
to check the source manifest age and every referenced object. Once the first complete mirror is available, set
`httpsManifests.factory.enabled` in the site's sources.json to true and deploy
the preview site. This also activates its browser requirement. Run
`node projects/factory-storyboard/verify-rollout.mjs` with Node 22.18 or later
to check the seven-minute limit and every object on the preview site.

### Withdraw a publication approval

1. Record the affected issue key, session ID in the public manifest, and old
   object paths from both the mirror and preview manifests. Include previous
   versions that carry the withdrawn text. Keep this inventory private.
   Remove the text from `APPROVED_HISTORY` and its fingerprint from the
   reader's `approvedHashes`. For a whole-session withdrawal, remove its
   archive entry and grants, and revoke the owner's share. Wait for both
   Kubernetes Secrets to reconcile. Roll the reader Deployment so it reloads
   its grants. Wait for a successful publisher Job started with the new Secret.
2. Fetch `https://factory-data.jomcgi.dev/manifest.json`. Record its
   `generatedAt`. For a whole session, check that its ID has disappeared
   from the session index and its events from the current issue timeline.
   For a partial withdrawal, read the replacement session object and check
   that the withdrawn turns are absent. Check all current references against
   the inventory and run `node projects/factory-storyboard/verify-rollout.mjs --mirror-only`.
3. If preview has served the affected snapshot, sync the Argo CD Application
   `jomcgi-site-dev` after the mirror check. Its Sync-hook Job builds a fresh
   Worker deployment from the mirrored manifest. Wait for the deploy and site
   checks to succeed. Fetch `https://next.jomcgi.dev/data/factory/manifest.json`
   and repeat the content and reference checks. Its `generatedAt` must be at
   least the mirror timestamp recorded in step 2. Run
   `node projects/factory-storyboard/verify-rollout.mjs` to check the digests.
4. Check the old keys separately. The publisher normally expires unreachable
   Garage objects once their last write is at least 24 hours old. The next
   mirror sync removes them from R2. For an urgent takedown, have the bucket
   operator remove the affected, unreferenced Garage keys immediately and
   confirm the deletions reach R2. Use authenticated listings of both buckets.
   Preserve objects still referenced by a current snapshot and resolve any
   remaining reference before deletion.
5. After origin removal and preview redeployment, have the Cloudflare zone
   operator purge the exact old object URLs and manifest URLs on both hosts.
   The mirror gives immutable objects a one-year cache lifetime. Check each
   original URL with an ordinary GET from outside the cluster. Expect HTTP
   404 or 410 for withdrawn objects at both `/objects/…` on the mirror and
   `/data/factory/objects/…` on preview. Repeat the manifest checks after the
   purge. If a deployment-specific Worker URL still serves an old copy,
   disable access to that deployment and verify its URL too. Keep that
   revision out of rollback while it contains withdrawn data. Copies already downloaded by readers can persist.

### Rollback of the database read access

To stop the storyboard reading the connector database, set `login: false`
for `factory_storyboard` in
`projects/platform-factory/factory-db/resources/cluster.yaml`. CloudNativePG
applies managed-role changes. Then end any open sessions, which keep
their access, and check that none remain:

```sh
KUBECONFIG=~/.kube/factory.yaml kubectl -n factory-db exec factory-db-1 -c postgres -- \
  psql -At -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = 'factory_storyboard'"
KUBECONFIG=~/.kube/factory.yaml kubectl -n factory-db exec factory-db-1 -c postgres -- \
  psql -At -c "SELECT count(*) FROM pg_stat_activity WHERE usename = 'factory_storyboard'"
```

The second command must print 0. Use the primary's pod name if
`factory-db-1` is not the primary. The
`storyboard_delivery` view and its grant stay, so a later redeploy works
without a new migration. To remove the grant as well, add a forward
migration that revokes it.
