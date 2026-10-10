# Local Grimoire table

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
The default table and its 29 session checks keep links disabled. Enrollment
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

The launcher warms the client hook with the running Vite server and waits for
its dependency scan and optimized files to finish before announcing readiness.
Each launch owns a fresh cache, including the telemetry imports that SvelteKit's
route-only scan otherwise discovers after the first page opens. Startup errors
fail the launcher; browser assertions and interaction deadlines are unchanged.

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
These identity endpoints live only in this development entrypoint, outside the
production source globs. Local runs disable browser telemetry instrumentation
and require no model, Authentik or cluster credentials. Fixtures use model
schema creation; production migration and real OIDC enrollment checks remain
separate from this simulated rehearsal.

A reviewed local snapshot is included in [evidence/report.json](evidence/report.json),
with provenance and screenshot checks in [evidence/review.json](evidence/review.json).
Inspect the [DM desktop](evidence/dm.png), [Bram phone](evidence/bram.png),
[player empty feed](evidence/a-empty-feed.png), [send error](evidence/player-send-error.png)
and [partial batch retraction](evidence/grouped-reveal-retracted.png).
This snapshot records a simulated run; rerun the command above for current evidence.
