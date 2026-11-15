# Local Grimoire table

Run the friends UI against the real Grimoire router and a disposable Postgres
16 database with pgvector. The UI is the React app in
`projects/monolith/web/grimoire`: the launcher builds it and the local backend
serves it with `grimoire/web.py`, as the production image does. One loopback
origin, `http://friends.localhost:8177`, serves the app and the invitation
landing under `/grimoire` and the API under `/api/grimoire`. There is no Node
server at run time. Three signed demo identities share one campaign:
Rowan (DM), Elowen (ranger), and Bram (fighter). Each launch starts fresh.

Install the repository's Node/pnpm tools with `./bootstrap.sh`, and install
Postgres 16 plus pgvector. On Ubuntu these are `postgresql-16` and
`postgresql-16-pgvector`. `GRIMOIRE_PG_BIN` selects another Postgres bin directory.
The launcher uses ports 8177 and 55477 and binds only to loopback.
`GRIMOIRE_WEB_DIR` names an already built bundle and skips the launcher's own
build.

The lobby supports campaign invitations, player character creation, and DM
assignment of existing or new characters. Clearing an assignment keeps the
character's sheets and knowledge grants. The rehearsal also creates a second
campaign, accepts two invitations, and creates characters from both roles.
Inspect `player-onboarding.png` and `dm-onboarding.png` for that flow.
Both players then submit sheets, the DM approves them, and the fresh table
starts a session with a quick roll using the approved modifier. Inspect
`nyx-submitted.png`, `wren-submitted.png`, `new-player-session.png` and
`new-dm-session.png` for those steps.

From the repository root, prepare Python 3.13 and the web app's dependencies:

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

The flag selects the link rehearsal instead of the default table and its 33
session checks. `report.json` lists passed checks in `checks` and any check the
app fails in `failed_checks`, with what was observed. A failed check that is
already understood is recorded and the rehearsal continues, so one defect does
not hide the checks after it; the run still exits nonzero. Enrollment stays disabled in both modes, and the launcher clears inherited provider token
and flow settings. These checks use signed local identities and never enroll a
real account.

The link rehearsal covers owner creation, hiding the one-time copy surface,
anonymous fragment capture, cookie attributes and resume, the wrong account,
close and browser back, a changed invitation in another tab, explicit acceptance,
retry, removal and replay, and revocation while acceptance is open. The
anonymous landing is the backend's own page (`grimoire/join_landing.py`); the
signed-in accept screen is the React route `/grimoire/join/accept`. It checks
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

The wrapper installs the pinned runtime and Playwright, builds the React bundle
once from the checkout (`web-build.log`), and runs the full browser scenario
twice with fresh databases. A third fresh launch runs the link scenarios. Every
stage runs even when an earlier one fails, so the run log and evidence are
complete; the wrapper then exits nonzero. Browser reports are under
`cold-start-1/` and `cold-start-2/`; `startup.log` counts the launches whose
shell and assets were served. Link reports, screenshots and traces are under `join-links/`, with
their launcher log in `join-links.log`. It removes only its
own temporary Python environment. The BuildBuddy PR gate calls `ci-runner.sh`
after Bazel for relevant changes. It provisions Ubuntu dependencies and uses
the repository tools image for Node/pnpm. Reports, screenshots, traces and logs
are uploaded from `BUILDBUDDY_ARTIFACTS_DIRECTORY/grimoire`, including on failure.
Root runners give the disposable PostgreSQL
directory and process to the system `postgres` account.

Before announcing readiness the launcher fetches the app shell and every script
and stylesheet it names. A bundle built for another base path, or a missing
asset, fails startup before any browser runs. Browser assertions and
interaction deadlines are unchanged.

This starts the whole table, exercises three separate browser contexts, saves
evidence to `/tmp/grimoire-rehearsal`, and stops. `--output /path` changes the
evidence directory. A failed check exits nonzero and preserves the report and
traces. To rehearse an already running table:

```bash
grimoire.venv/bin/python projects/monolith/dev/grimoire/rehearse.py
grimoire.venv/bin/playwright show-trace /tmp/grimoire-rehearsal/dm-trace.zip
```

Inspect `report.md` for linked screenshots and traces, and `report.json` for
machine-readable checks and timings. A failed session rehearsal also saves each
open page's accessibility tree as `<seat>-final-<n>.aria.yml`. Inspect `dm.png`, `elowen.png`, `bram.png`, and
`player-reconnecting.png`. The rehearsal checks message audiences in rendered
pages and in the seat's own `/api/grimoire` reads (the lobby, sessions,
characters, feed, turn order and both journals), denied player mutations, phone overflow,
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

The launcher serves its own ephemeral JWKS and signs local ID tokens.
`/__local/login/{role}` stands in for the gateway's login: it sets the signed
token in the `grimoire-id-token` cookie for this origin, and the launcher sets
`GRIMOIRE_BROWSER_ORIGIN` to the same origin so the backend accepts it. The
normal backend signature, issuer, audience and campaign checks still run, and a
write must carry the matching `Origin`, as in production.
These identity and embedding endpoints live only in this development
entrypoint, outside the production source globs. Local runs require no model,
Authentik or cluster credentials. Fixtures use model schema creation;
production migration and real OIDC enrollment checks remain separate from this
simulated rehearsal.

The `evidence/` snapshot predates the React app: it records the Svelte UI at
the commit named in `review.json` and has not been regenerated.
A reviewed local snapshot is included in [evidence/report.json](evidence/report.json),
with provenance and screenshot checks in [evidence/review.json](evidence/review.json).
Inspect the [DM desktop](evidence/dm.png), [Bram phone](evidence/bram.png),
[player empty feed](evidence/a-empty-feed.png), [send error](evidence/player-send-error.png)
and [partial batch retraction](evidence/grouped-reveal-retracted.png).
This snapshot records a simulated run; rerun the command above for current evidence.
