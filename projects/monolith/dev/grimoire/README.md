# Local Grimoire table

## Rolling-deployment recovery status

Issue #7565 remains open. The BFF's GET retry is staged behind the frontend
environment variable `GRIMOIRE_ROLLOUT_RECOVERY_ENABLED=true`, which defaults
off and is not enabled in the deployment chart. It permits one additional
attempt for a fetch connection failure or HTTP 502/503/504. Both attempts share
a 1.5-second deadline. Caller cancellation still applies. Mutations retain
their ten-second deadline and are never automatically retried. Authorization,
validation, application errors and invalid JSON are not retried.

Run the focused regression from the repository root:

```bash
pnpm --dir projects/monolith/frontend exec vitest run src/lib/grimoire src/lib/server/grimoire-auth.test.js src/lib/server/grimoire-session.test.js src/hooks.client.test.js
```

The existing `//projects/monolith/frontend:test` Linux target discovers the
tests through its `src/**/*.test.js` glob. The session DOM tests mount the real
page and execute the inline `app.html` callback through `hooks.client.js`.
They cover transient poll/send classification, uncertain-send request IDs,
chunk fallback and restored forms. They are not a process-restart or rollout
rehearsal. The three-seat built-asset swap and backend-restart rehearsal remain
outstanding. The current launcher below uses a dev frontend and cannot prove
built-chunk replacement recovery. Its backend also creates a fresh disposable
signing key on startup; a restart rehearsal must retain that local key and
database without using live credentials.

Operational acceptance remains separate after repository work is finished.
Within 72 hours of an authorized rollout, its owner must call
`verify_deployment` with `app="monolith"` and
`expected_revision="<CI-produced chart version containing the delivered head>"`
and retain the verified revision and timestamps. The owner must also read a
revision-bound report of one actual rollout during an active DM/two-player
session. That report must include rollout start/end times, each client's
recovery duration, before/after values for every exercised draft, exact event
and request IDs (including a committed send whose response was lost), stored
event deduplication results, and error/sign-out observations. Require no lost
or duplicate events, every draft retained, and all clients Live within ten
seconds. Local reports cannot satisfy this live acceptance. A command for
collecting that live report has not yet been implemented.

### Staged client recovery

The session server load exposes only the boolean `rolloutRecoveryEnabled`,
derived from `GRIMOIRE_ROLLOUT_RECOVERY_ENABLED === "true"`. It never exposes the
environment or authentication material. With the flag off, the existing BFF,
send-error and generic chunk-reload paths remain in use. No chart setting is
changed by this delivery.

With the flag on, connection failures and HTTP 502/503/504 are Reconnecting,
including HTML proxy responses. A non-auth transient failure clears the old
auth-expiry observation; a successful poll returns Live. Real 401/manual
login redirects still require the existing two observations before sign-out.
Validation and application errors remain visible. No POST is retried
automatically. An uncertain composer/handout send retains its exact request ID
for a manual deduplicated retry.

The mounted session registers a save callback with `window.__freshReload`.
`hooks.client.js` first attempts SvelteKit `preloadCode` and `goto` for a failed
navigation. Preloading has a 1.5-second bound. An error on the current route
(including a lazy panel without an in-place retry callback), a rejected load,
or an obsolete build falls back to the guarded fresh-HTML reload. The existing
30-second reload guard and unrelated-page navigation targets remain intact.
The callback saves before attempting recovery and immediately before replacing
the location. A failed save blocks location replacement and shows a
copy-before-reload notice while the session is mounted. SvelteKit retains its
own error-page handling for a failed navigation. An in-progress image upload
also prevents replacement.

The per-tab snapshot uses sessionStorage key `grimoire-rollout-drafts:v1`, a
96,000-byte total limit and a 15-minute lifetime. It is bound to current viewer,
role, character assignment, campaign, session and the server's permission,
character and speaker projection. Ended sessions and mismatched identities or
permissions discard it. The paired composer uses the existing `keepDraft` path
and additionally checks the session. Corrupt/version-mismatched/expired storage
is discarded; unavailable or quota-exhausted storage cannot trigger a loop or
claim that drafts were saved. Restoration runs after initial page effects and
panel reads; note edit rights and current inventory items are checked again.

| State | Saved across the guarded session reload |
| --- | --- |
| Composer | Text, audience, reply, speaker, resolved choice and pending request ID/signature |
| Page | Open tab, journal view, dice formula/label/mode/visibility/type, cheap details and scroll position |
| Notes | Audience, note ID, title, markdown, DM sharing and pin choice; edits require current edit permission |
| Handout | Title, markdown, recipients, completed upload key/metadata and pending request ID/signature; never file bytes or an in-flight upload |
| Initiative | Dirty rows, active row and hidden-entry display; restored dirty so polling cannot overwrite it |
| Voice | Speaker/free label, hints, rate/pitch and NPC query; a picked NPC is read again through the current authorized endpoint |
| Inventory | Give form plus item edit/quantity/move/reason drafts; only currently projected items and available owners restore, after initial reads |
| Knowledge | Unsent search query and reveal choices/clue; selected entity is read again, old search results and previews are omitted |
| Read aloud | Preferences only; existing events are marked handled on mount, with no queue or utterance replay |

Hidden/unmounted inventory forms stay in the page registry when switching tabs.
Restoration never submits a form, emits an event or resumes speech. Backend
state, event feeds, audit/history results, credentials and private search/entity
projections are not stored in the snapshot. The next implementation must still
exercise every draft with three real old clients during the executable
backend-restart/production-build swap, retain signing identity/database, and
provide revision-bound live-report collection. No such rehearsal ran in the
client implementation node.

## Start a disposable table

Run the friends UI against the real Grimoire router and a disposable Postgres
16 database with pgvector. Three signed demo identities share one campaign:
Rowan (DM), Elowen (ranger), and Bram (fighter). Each launch starts fresh.

Install the repository's Node/pnpm tools with `./bootstrap.sh`, and install
Postgres 16 plus pgvector. On Ubuntu these are `postgresql-16` and
`postgresql-16-pgvector`. `GRIMOIRE_PG_BIN` selects another Postgres bin directory.
The launcher uses ports 4177, 8177 and 55477 and binds only to loopback.

The lobby supports campaign invitations, player character creation, and DM
assignment of existing or new characters. Clearing an assignment keeps the
character's sheets and knowledge grants. The rehearsal also creates a second
campaign, accepts two invitations, and creates characters from both roles.
Inspect `player-onboarding.png` and `dm-onboarding.png` for that flow.
Both players then submit sheets, the DM approves them, and the fresh table
starts a session with a quick roll using the approved modifier. Inspect
`nyx-submitted.png`, `wren-submitted.png`, `new-player-session.png` and
`new-dm-session.png` for those steps.

From the repository root, prepare Python 3.13 and frontend dependencies:

```bash
uv venv grimoire.venv
uv pip install --python grimoire.venv/bin/python --no-deps -r bazel/requirements/runtime.txt
uv pip install --python grimoire.venv/bin/python playwright==1.63.0
grimoire.venv/bin/playwright install chromium
pnpm install --frozen-lockfile
```

The runtime lock is installed with `--no-deps`, matching the repository's
locked package set and its dependency overrides.

Open the local table:

```bash
grimoire.venv/bin/python projects/monolith/dev/grimoire/run.py
```

Visit `http://friends.localhost:8177/__local` and choose a seat. Use separate
browser profiles for the DM and players. Stop with Ctrl-C; the launcher stops
its processes and removes its database. The current play screen covers
narration, public and private player actions, targeted DM messages, and session
start, pause, resume and end. Players have approved example sheets with quick
ability checks and saving throws. Rolls support public and private audiences,
modifiers, advantage and disadvantage. The DM can reply privately to a waiting
player action and mark it resolved.

Run and inspect the browser rehearsal:

```bash
grimoire.venv/bin/python projects/monolith/dev/grimoire/run.py --rehearse
```

Rehearse single-use links for registered players in a fresh local campaign:

```bash
grimoire.venv/bin/python projects/monolith/dev/grimoire/run.py --rehearse --join-links --output /tmp/grimoire-join-links
```

The opt-in flag enables links only in this disposable frontend and backend.
The default table and its 33 session checks keep links disabled. Enrollment
stays disabled in both modes, and the launcher clears inherited provider token
and flow settings. These checks use signed local identities and never enroll a
real account.

The link rehearsal covers owner creation, hiding the one-time copy surface,
anonymous fragment capture, cookie attributes and resume, the wrong account,
close and browser back, a changed invitation in another tab, explicit acceptance,
retry, removal and replay, and revocation while acceptance is open. It checks
membership through the real backend after each mutation. Chromium treats the
`friends.localhost` loopback host as trustworthy for Secure cookies; the checks
assert the cookie is actually stored and can resume the invitation.

Inspect `join-owner-links.png`, `join-public-resume.png`, `join-wrong-account.png`,
`join-changed-tab.png`, `join-review.png`, `join-accepted.png`,
`join-replay-blocked.png` and `join-revoked.png`. Failure screenshots mask the
one-time link field, and reports redact capability-shaped values. Traces include
only disposable local credentials and links. Review evidence before checking
in any assets. Production migration and real OIDC enrollment remain separate
checks.

For a clean Python environment, use the CI wrapper after installing `uv`, pnpm,
Postgres 16 with pgvector, and Chromium's system libraries:

```bash
GRIMOIRE_EVIDENCE_DIR=/tmp/grimoire-ci-evidence projects/monolith/dev/grimoire/ci.sh
```

The wrapper installs the pinned runtime and Playwright, checks three cold frontend
starts, and runs the full browser scenario twice with fresh databases and dependency
caches. A third fresh launch runs the opted-in link scenarios. It preserves the
run log and evidence even on failure. Browser reports
are under `cold-start-1/` and `cold-start-2/`; dependency-readiness checks are in
`startup.log`. Link reports, screenshots and traces are under `join-links/`, with
their launcher log in `join-links.log`. It removes only its
own temporary Python environment. The BuildBuddy PR gate calls `ci-runner.sh`
after Bazel for relevant changes. It provisions Ubuntu dependencies and uses
the repository tools image for Node/pnpm. Reports, screenshots, traces and logs
are uploaded from `BUILDBUDDY_ARTIFACTS_DIRECTORY/grimoire`, including on failure.
Root runners give the disposable PostgreSQL
directory and process to the system `postgres` account.

The launcher transforms the factory markdown renderer
(`src/lib/public/factory/markdown.js`) with the running Vite server and waits for
the dependency scan and optimized files to finish before announcing readiness.
Each launch owns a fresh cache. A broken client import fails startup without
publishing readiness; browser assertions and interaction deadlines are unchanged.

This starts the whole table, exercises three separate browser contexts, saves
evidence to `/tmp/grimoire-rehearsal`, and stops. `--output /path` changes the
evidence directory. A failed check exits nonzero and preserves the report and
traces. To rehearse an already running table:

```bash
grimoire.venv/bin/python projects/monolith/dev/grimoire/rehearse.py
grimoire.venv/bin/playwright show-trace /tmp/grimoire-rehearsal/dm-trace.zip
```

Inspect `report.md` for linked screenshots and traces, and `report.json` for
machine-readable checks and timings. Inspect `dm.png`, `elowen.png`, `bram.png`, and
`player-reconnecting.png`. The rehearsal checks message audiences in rendered
pages and response payloads, denied player mutations, phone overflow,
polling recovery with an unsent draft, and the session lifecycle.
It also drops a successful send's response, checks that the draft and retry
guidance remain, and retries without duplicating the stored event. Message
request IDs are scoped to their author and session; changing a message starts
a new request. The rehearsal repeats pause, resume and narration on a DM phone
viewport. Inspect `player-send-error.png` and `dm-phone-controls.png`.
Timing gates require normal visible-tab delivery, foreground catchup and reveal delivery within three
seconds, and the automated reveal interaction within fifteen seconds. These
measure simulated local interactions, rather than human task completion time.
It checks that incoming events preserve composer focus and that incoming
knowledge preserves the position and keyboard focus of a reader viewing older
events. It also verifies private reply resolution, server-generated dice totals, and
quick rolls using the approved character sheet's persisted bonuses.
Grant creation and scope upgrades append audience-filtered knowledge reveals
during play. The rehearsal creates a partial NPC grant and checks the recipient's
feed while excluding a private DM canary from player payloads. The in-session
DM editor searches knowledge, filters previously granted entities, selects
recipients and shares a name, selected detail or full knowledge. It also
retracts grants, with a silent option. The rehearsal exercises the editor,
retraction and sharing one entity with both players in a single transaction.
The bulk API groups several entities for one character into one reveal event.
The rehearsal seeds a two-entity batch through that API, inspects and pins the
result through the player UI, then retracts one item through the DM editor.
The other item stays visible in the feed, journal and new pins. Original
projections remain in the DM audit; player responses omit removed items.
Inspect `grouped-reveal.png` and `grouped-reveal-retracted.png`.

Four checks cover combat, handouts, inventory and player search. The DM orders
Elowen, Bram and a hidden monster, sorts by initiative and advances turns. Only
the active player sees the turn banner. Both players see `???` for the monster,
and its label never reaches their pages or state payloads. Each player's order
carries only their own character id. The DM then ends the encounter. The DM
sends a text handout to the table and a private one to Elowen. Bram never
receives the private one; Elowen pins it to her notes. Give item turns the table
handout into a party-pool item that both players see in the Inventory tab. A
hidden party item and its id stay out of player pages, item lists, change
history and state. Those views must still carry the public item, so a failed or
empty response cannot pass. Each of these player phone views must fit without
sideways scrolling.

The launcher points `EMBEDDING_URL` at `/__local/v1/embeddings`, a deterministic
word-hash stand-in for the voyage model. The real embedding client, knowledge
search route and pgvector query run against it. Production embeds play state on
the five-minute `grimoire-embed-play` schedule; the rehearsal runs that job
through `POST /__local/embed-play`. Before that run, the DM grants Bram full
knowledge of Harbourmaster Ilsa. Bram's search for Mara returns his partial
reveal, and his search for Ilsa's secret finds it. The DM then retracts the Ilsa
grant; with no further embedder run, the secret is gone from his search response
and page. A DM-only narration canary and Elowen's private handout never appear
in his results. The DM and Elowen searches return their own canaries as positive
controls. Vector search has no score threshold, so a canary query still returns
the nearest visible hits. Inspect
`dm-initiative.png`, `bram-your-turn.png`, `elowen-handouts.png`,
`dm-inventory.png`, `bram-inventory.png` and `bram-knowledge-search.png`.

The Notes tab supports private and party notes, markdown, editing, deletion,
pinning, and explicit per-note DM sharing. Feed events can be pinned as private
notes using only their visible projection, with a link back to the source event.
The browser rehearsal checks privacy, opt-in sharing, party visibility,
deletion, note draft retention across Story/Notes tabs, and pinned knowledge.

The Journal tab derives Learned, Received, People and places, Rolls, and Open
threads from the viewer's visible events on each feed refresh. Party journal
uses only table events. Retractions and resolved private actions update the
journal automatically; no model or stored summary is involved. The API also
provides session journals across a campaign with session pagination.

Knowledge can be explored from reveal cards, pinned-note entity chips and
journal entries without discarding the session draft. The drawer and standalone
campaign entity page both use the backend's current viewer projection. The DM
can select individual detail fields, preview each recipient's exact server
projection, then confirm a reveal. The rehearsal checks partial A/full B views,
denied ungranted entity pages, and knowledge navigation from notes and journals.

The DM's standalone grants matrix shows entities by character, with name, type
and granting-session filters. Each cell opens an editor with a server-backed
preview, scope/detail changes and retraction. Partial detail edits replace old
reveal snapshots, and reduced scopes remove broader data from the player feed
and journal. The rehearsal exercises editing, scope reduction and retraction
while confirming players cannot open the matrix.

The launcher serves its own ephemeral JWKS and signs local ID tokens. The
normal backend signature, issuer, audience and campaign checks still run.
These identity and embedding endpoints live only in this development
entrypoint, outside the production source globs. Local runs require no model,
Authentik or cluster credentials. Fixtures use model schema creation;
production migration and real OIDC enrollment checks remain separate from this
simulated rehearsal.

A reviewed local snapshot is included in [evidence/report.json](evidence/report.json),
with provenance and screenshot checks in [evidence/review.json](evidence/review.json).
Inspect the [DM desktop](evidence/dm.png), [Bram phone](evidence/bram.png),
[player empty feed](evidence/a-empty-feed.png), [send error](evidence/player-send-error.png)
and [partial batch retraction](evidence/grouped-reveal-retracted.png).
This snapshot records a simulated run; rerun the command above for current evidence.
