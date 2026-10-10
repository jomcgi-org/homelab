# Grimoire architecture

Grimoire is a domain of the Python Monolith. The former standalone Go API,
React frontend, WebSocket gateway, Redis service, Helm chart, and GCP bootstrap
have been retired. Their implementation remains available in git history.

## Runtime shape

**Why.** Audience is copied onto play embedding rows so the kNN filter uses one
predicate. Reads re-check the live source row. Embedding notes and player-safe
event projections is offloaded to the five-minute `grimoire-embed-play`
CronWorkflow (#6626).

`GET /api/grimoire/campaigns/{campaign_id}/knowledge/search` is play-gated and
available to every campaign member, including members without a character.
It accepts a 1 to 200 character query and returns at most `k` (1 to 50, default
10) scored hits with `type` (`entity`, `note`, `event`, or `chunk`) and `source`
link fields: `entity_id`, `note_id`, `session_id` plus `seq`, or `book_id` plus
`chunk_id`. The existing corpus `/search` endpoint is unchanged.

**Why.** The candidate predicate uses the copied audience columns and the
existing audience and note SQL contracts. Each candidate is resolved from the
live source and checked again, including soft deletion, retraction and campaign
scope. Partial grants expose only revealed details; recognition-only entities
and reveal items are excluded from retrieval. A private note belongs to its
author's knowledge, with DM access requiring `dm_readable`.

- `grimoire/module.py` composes the domain into the private and public Monolith
  profiles.
- Private routes live under `/api/grimoire` in `router.py`.
- Read-only public routes use the same prefix through `router_public.py`.
- The Svelte frontend lives in `projects/monolith/frontend/src/lib/grimoire`
  and `projects/monolith/frontend/src/routes/public/app/grimoire`.
- Persistent state lives in the Monolith Postgres cluster. There is no separate
  Grimoire deployment or datastore.

## Data model

The `grimoire` schema uses a typed entity spine rather than the standalone
prototype's Firestore and polymorphic JSON model:

- `entity` stores shared identity, provenance, visibility, and hierarchy.
- `entity_creature`, `entity_spell`, `entity_location`, and `entity_npc` hold
  type-specific queryable fields.
- `knowledge_chunk`, `chunk_entity_mention`, `chunk_extraction`, `relationship`,
  and `embedding` provide corpus, graph, extraction, and retrieval state.
- `alias_candidate` records review evidence, state hashes, explicit approvals,
  and completed alias-merge provenance.
- `book` and `adventure` organize source material.
- `campaign`, `player_character`, `game_session`, `session_event`, and `knowledge_grant` hold
  mutable play state and per-player knowledge visibility.

Queryable values use typed columns. Irregular display-only structures may use
JSON. Embeddings share one pgvector-backed retrieval surface.

## Visibility and public access

Private DM routes can read the complete corpus. Player-scoped reads centralize
the `is_global OR granted-to-player` rule and apply the grant scope when
projecting details. Public corpus routes are read-only. Full text and page
images fail closed unless the book is explicitly classified as open-licensed;
copyrighted books expose only derived entities, graph structure, and bounded
snippets.

## Audience contract

`Audience` in `audience.py` has three values: `table`, `dm`, and `pcs` with a
non-empty set of player-character ids. DMs see every row. A player with a
character sees table rows, rows addressed to that character, and authored rows
with a recognized audience. A characterless member sees only table rows, even
when they authored a restricted row. Non-members cannot use the contract;
routes authorize membership and scope the campaign first.

Play tables store `audience` as a string, `audience_pc_ids` as JSONB on Postgres
and JSON on SQLite, and nullable `author_member_id` as a UUID on Postgres and
a 36-character string on SQLite. `Audience.to_columns()` validates the kind and
sorts and deduplicates character ids. `audience_predicate` filters SQL rows;
`can_see` checks the same policy in Python. The seeded agreement matrix runs
against SQLite and real Postgres, including exact JSON membership and unknown
audiences. Unknown kinds fail closed for non-DMs.

`testing/leak_harness.py` seeds private entities, grants, character sheets,
administrative rows, relationships, and another campaign with per-field wire
canaries. `route_inventory_test.py` registers both production routers and
checks every campaign route against its explicit `CASES` table. Each case has
an authorized success control, denied callers, cross-campaign resource checks,
and database snapshots for rejected writes. Every new campaign route needs a
`CASES` entry. Every new play table filters with `audience_predicate`, composed
with its campaign predicate. Existing corpus entities retain the grant overlay
in `visibility.py`.

## Session event log

`grimoire.session_event` stores each session's ordered events: campaign and
session ids, a positive `seq`, kind, author member, audience columns, JSON body,
creation time, and optional retraction time. The unique `(session_id, seq)`
index serves the feed's ascending keyset query. Polls select `seq > after` and
apply `audience_predicate` in SQL before the limit, default 100 and maximum 500.
The DM sees retracted bodies; other admitted viewers receive a null body and
the retraction timestamp. Excluded viewers receive no event. Player responses
omit other members' author ids and other characters' audience ids.

`session_events.append_event` is the single insertion path for HTTP actions,
the server-side roller (#6611), and utterance ingest (#6618). It locks and
refreshes the game-session row, refuses ended sessions, validates the audience
against that campaign, and allocates `max(seq) + 1` inside the caller's
transaction. The helper flushes; the caller commits or rolls back. Retractions
retain the row and sequence, lock the event row, and are idempotent, including
after the session ends.

**Why.** A Postgres sequence consumes numbers on rollback and would leave gaps.
Locking the existing session row serializes writers within one session while
other sessions can append independently. Allocation and insertion roll back
together. The unique index also rejects a writer that bypasses serialization.

The six play HTTP routes require `GRIMOIRE_PLAY_ENABLED` to equal the lowercase
string `true`, read on each request before identity and membership checks.
`grimoire.play.enabled` defaults to false in the chart; deployment values stay
off. Existing session creation and status updates are unaffected. Campaign
members may list sessions and poll events. DMs may append every kind except
`utterance` and `roll`; players may append only `action` with `dm` or `table`
audience. HTTP utterances are refused for every caller because #6618 owns
ingest. Generic event writes refuse `roll` for every caller with 403 because
only the server-side roller may produce trusted roll bodies.
DMs may retract any event; players may retract only their authored actions.

`POST /campaigns/{campaign_id}/sessions/{session_id}/rolls` rolls on the server
and appends a `roll` event with the authenticated member as author. Its body
contains the normalized formula, total, every die in roll order, the kept dice,
flat modifier, optional label, and requested or default visibility. The ASCII
grammar accepts `NdM`, optional `khK`, `klK`, `adv` or `dis`, and one optional
signed flat modifier. Raw formulas are limited to 64 characters, 1 to 100 dice,
1 to 1000 sides, and modifier magnitude 1000. Keep counts must be positive and
clamp to the dice count. Advantage and disadvantage require exactly one die,
roll twice, and keep the higher or lower die. Outer whitespace and ASCII case
are normalized; internal whitespace, line breaks, and non-ASCII input are
rejected. Labels are stripped and limited to 200 characters. Invalid formulas
return 400 with a reason, after membership and session scope checks; ended
sessions return 409. Rejected requests write no event.

DM rolls default to `dm`; player rolls default to `table`. Table visibility
uses the table audience. DM visibility uses the dm audience with author
provenance, so a player with a character and the DM both see that player's
hidden roll. Self visibility uses `pcs` containing the player's character
with the same author provenance. A DM choosing self uses the dm audience.
Characterless players may roll only table; restricted visibility returns 422
because their audience contract would hide even their own restricted roll.
These rules leave `audience_predicate` unchanged. There are 44 campaign routes
in the route inventory, including the roller and bulk grants.

**Why.** Server-side `secrets.SystemRandom` prevents clients from supplying
results or seeds. A dependency override lets tests use a seeded RNG and assert
the exact stored body without changing production randomness. Characterless
players' table-only rule keeps every accepted roll visible to its roller.

Part of #6610, which owns coordinated enablement of `grimoire.play.enabled`
and live audience checks after the #6612 play surface is ready. Deployment
values remain off. No operational flag flip belongs to this change.

## Journal

Both journal routes are play-gated and computed on read. The session route
returns `learned`, `received`, `people_and_places`, `rolls`, and `open_threads`.
The campaign route returns `{sessions: [{session_id, started_at, journal}],
next_cursor}`. Sessions are newest first with a start-time/ID keyset cursor,
10 sessions by default and at most 50. One audience-filtered event query
loads each page, capped at the earliest 500 visible events per session
(`JOURNAL_EVENTS_PER_SESSION`); a journal folded over a capped stream
reports `truncated: true` instead of silently dropping the tail.
`view=mine` is the default; `view=party` includes table events
only, leaves Learned and Open threads empty, and includes everyone's table
rolls. Characterless campaign members can use both views.

**Why.** A projection over session events avoids a stored journal copy and a
second retention policy. Membership and UUID/session scope run as dependencies
before query validation. SQL uses the feed's campaign, session and
`audience_predicate` filters; the pure projection applies `can_see` again and
discards every retracted row, including for the DM.

Learned folds single and bulk reveal bodies by target PC and entity in sequence
order. The latest visible live reveal supplies the identity, scope and snapshot.
DM entries include `player_character_id`; player entries never carry other PC
or member attribution IDs. Non-silent revocation keeps an identity-only entry
marked `retracted`. Silent revocation drops entries whose current
`KnowledgeGrant` pair no longer exists. A later re-grant restores the entry.
Downgrades emit no event: Learned records what the player saw, including a
historical snapshot that can be broader than today's grant.

**Why.** Silent revocation events deliberately contain no entity identity.
Current grant pairs remove the earlier live snapshot without adding identity
to the revocation event. Retraction entries exclude all entity details.

Narration may carry `body.entity_ids`, a list of dashed UUID strings. Malformed
items are ignored. People and places deduplicates surviving Learned identities
and those explicit narration references. Narration identities come only from
`visible_entities_query` and `project_entity` in relationship context, restricted
to referenced IDs, so name-only recognition can contribute a name stub. It
never matches narration text against names or reads names from raw entity rows.

**Why.** Learned identities preserve the reveal the viewer actually saw.
Narration references pass today's visibility overlay before yielding a name.
Private IDs cannot turn a table narration into an entity lookup bypass.

A reply may carry `body.reply_to`, the exact ID of an earlier event in the same
session. A later visible, unretracted reply closes an authored non-table action
in Open threads. Received carries visible handout bodies; Rolls carries the
member's own rolls in mine view. Event entries expose author and audience PC IDs
under the feed's DM-or-self rules.

**Why.** A hidden or retracted reply cannot change a player's journal state.
Reply ordering uses the session sequence, so an earlier reference cannot close
a later action. Live enablement and audience checks remain owned by #6610.

The UI ships a props-driven `JournalPanel` and standalone campaign journal page; mounting the session-screen Journal tab and refreshing it with each feed poll remains in #6808, blocked by #6612, per the #6616 notes precedent.

## Grant changes

Grant changes emit `reveal` events only when play is enabled and the campaign
has an active or paused session. Creation defaults an omitted
`granted_in_session` to that session; an explicit session is preserved, while
the event always targets the current session. Grant and event share one
transaction. The event's author is the DM member and its audience is the
grantee PC. Scope upgrades emit only for a strict increase in
`name_only < partial < full`, after applying `revealed_details`. Downgrades,
same-scope edits and details-only edits emit nothing.

Each reveal body has exactly `entity_id`, `name`, `entity_type`, and
`grant_scope`. Full and partial bodies also have `entity`, computed through
`project_entity` for the persisted grant's grantee in lookup context. Partial
projections contain only `id`, `entity_type`, `name`, and `revealed_details`;
full projections include typed detail and JSON-encoded datetimes. Name-only
bodies carry no projection. Deleting a grant writes the identity keys with
the previous scope plus `retracted: true` and `silent: false`. A silent delete
stores exactly `{"retracted": true, "silent": true}`. Original reveal rows
and their retraction timestamps remain unchanged.

The DM-only, play-gated bulk grant route accepts 1 to 50 unique entity/PC
pairs sharing one entity or one PC. It validates every item before writes,
inserts all grants, appends one event per distinct PC with
`{"reveals": [<per-entity body>, ...]}` in request order, and commits once.
Failures roll back both grants and events.

On entities and search, DM-only `not_granted_to` means "hide entities that PC
already knows": exclude global entities and any grant scope using a
correlated grant alias in the shared visibility query. Search chunk hits are
unaffected. Authorization checks membership, play flag, DM role, then PC
campaign membership, in that order. With the flag off, the new filters and
bulk route return 404 and existing grant and read behavior is unchanged.
Live enablement and live audience checks remain in #6610.

**Why.** The grantee projection bounds the stored feed snapshot. One
transaction keeps knowledge and feed changes together; an identity-free
silent retraction leaves no entity data for a read path to expose. Sharing
the visibility predicate keeps reveal search aligned with player knowledge.

## Notes

`grimoire.note` stores character and party notes, with an opt-in `dm_readable`
flag and soft deletion. `note_predicate` and `can_see_note` extend the audience
contract with a separate policy: a character note belongs to its author, even
after that member loses their character. The DM sees it only when `dm_readable`
is true. Party notes are visible to the DM and members currently holding a
character. Characterless members see no party notes. NULL authors match nobody;
unknown kinds and deleted rows are invisible to everyone, including the DM.
Every query also filters the campaign. Seeded SQLite and PostgreSQL agreement
matrices pin the SQL and Python policies against an independent oracle.

The notes API works independently of `GRIMOIRE_PLAY_ENABLED`. Membership and
the current character determine the viewer. Players with a character may create
either kind; DMs may create party notes only. Character notes may be edited or
deleted only by their author; party notes by their author or the DM. Only a
character note's author may change its DM sharing flag. A DM-only settings
route updates `notes_dm_readable_default`, initially false, which applies only
when creation omits the flag. Explicit false remains private. Invisible ids
return the same 404 as random ids. Search escapes LIKE wildcards and applies
visibility and substring matching in SQL before ordering and limiting rows.

Entity links are checked against the author's grant overlay at write time and
resolved again for each viewer at read time. Chips expose only id, name and
type, dropping invisible entities entirely. Event ids are opaque UUID strings,
stored without a foreign key or event lookup. Author and character ids go only
to the author and DM; the sharing flag goes only to the author. A viewer-computed
`can_edit` flag exposes edit capability without adding author identity to another
player's projection. There is no `public_reader` grant on notes.

The friends campaign notes route loads and writes through the server's dedicated
Grimoire token, validates campaign and form UUIDs, and disables caching. Failed
reads reject the load rather than returning an empty list. `NotesPanel` takes
notes, viewer capabilities, filters and form actions as props, with no client
API calls. Mine and Party tabs forward `kind` and `q`. Quick-add offers Campaign
default (omits `dm_readable`), Private and Share with DM. DMs see Shared with you
and can only add party notes. Markdown uses the existing text-node renderer;
entity chips come only from resolved viewer-visible objects. Feed pinning and
session-screen mounting remain in #6689, blocked by #6610 and #6612.

**Why.** Notes are player-owned records. DM access to generic play rows does not
authorize access to a private character note. Read-time chip resolution retains
each viewer's entity grants without sharing another player's identity.

## Ingestion

Batch commands in `app/jobs_main.py` invoke the domain jobs:

- `grimoire-load-chunks` validates externally produced chunk manifests and
  loads books, adventures, chunks, and embeddings.
- `grimoire-extract-entities` produces typed entities, mentions, and graph
  relationships.
- `grimoire-backfill-hierarchy` repairs or derives entity hierarchy data.

The jobs are discrete read, compute, and write stages with recorded provenance.
Bad inputs fail or dead-letter without partially publishing a book.

## Alias review and merge

The private API exposes the report-first alias pass from ADR services/014:

1. `POST /api/grimoire/alias-candidates/scan` refreshes candidates from
   same-type, same-book short/full-name pairs with co-mention evidence.
2. `GET /api/grimoire/alias-candidates?status=pending` returns the durable review
   queue, including bounded source snippets and the exact state hash.
3. After inspecting that evidence, a verified standing human in the `operators`
   group may call `POST /api/grimoire/alias-candidates/{id}/approve` with
   `survivor_entity_id` and the report's `expected_state_hash`. Reviewer
   attribution comes from the verified principal, never request data.
4. `POST /api/grimoire/alias-candidates/{id}/execute` revalidates the approval,
   rewrites the graph in one transaction, and refreshes the survivor embedding
   when its persisted mention-summary input changes.

The same authorized human may persist a version-bound rejection through
`POST /api/grimoire/alias-candidates/{id}/reject`. Scans refresh its evidence but
never silently reopen it or make it executable. Returning a rejected or stale
candidate to review requires a deliberate, version-bound
`POST /api/grimoire/alias-candidates/{id}/reopen` call. All alias report,
decision, scan, and execution routes require the same standing-human operator
authorization.

Scanning never approves or merges a pair. Approval records the reviewer, time,
survivor, and reviewed state hash. Any later entity, detail, mention, type, book,
site, temporality, or evidence change makes that approval stale and requires
another review.
Execution is replay-safe: a completed candidate returns its existing merged
status, while embedding or database failures leave an approved pair retryable.
The endpoints are registered only on the private Grimoire router.

## Loom compatibility

Postgres is the current source of truth and serving tier. The schema remains
compatible with a future Loom/Iceberg durable tier:

- typed entity tables map to typed object datasets;
- `relationship` maps to link definitions;
- `knowledge_grant` represents the materialized per-player slice;
- embeddings retain model and source lineage;
- session-scoped mutable rows form a potential check-in delta.

Loom checkout and check-in are deferred. They are not part of the active
runtime, and no current code should imply otherwise.

## Deliberately absent

The retired prototype's Cloud Run services, Firestore, Redis fan-out,
standalone authentication, Gemini Live voice pipeline, and separate React UI
are not supported architecture. New Grimoire work belongs in the Monolith
domain, its Svelte frontend, or its batch jobs.

## Friend onboarding and campaign ownership

The friend UI lives at `https://friends.jomcgi.dev/grimoire`. Its dedicated
Authentik provider and OIDC cookies are separate from the operator and moving
applications. Only the Svelte BFF is exposed on this path. It forwards the
signed Grimoire ID token, which the backend independently checks against the
Grimoire issuer, audience, signature and expiry. This provider is not added to
the shared operator/MCP token resolver.

Account signup remains administrator-controlled. The lobby does not send email
or send campaign owners into Authentik's global invitation administration.
With campaign links disabled, the existing registered-player invitation and
acceptance workflow remains available. Existing human Authentik users can sign
in directly; administrator-managed account enrollment is separate until the
optional integration has passed its access and rollout review.

First login creates the Grimoire account. Subsequent logins update its email
and display name using the verified `(issuer, subject)` identity. Memberships,
ownership and invitations reference the account ID, not the email. Legacy
email-only rows can be linked by a verified mailbox or their existing trusted
Cloudflare identity. An email collision between established identities fails
closed and requires explicit operator reconciliation; signing in through a
new provider never silently takes over another identity's memberships.

Every registered user can create a campaign and becomes its owner and initial
DM. Owners invite registered players by exact email, without a user directory.
Recipients must accept in their lobby before any campaign data becomes
available. Decline, cancellation and membership revocation are durable;
re-inviting creates a fresh invitation ID so an old acceptance cannot be
replayed. Ownership controls invitations and membership removal; the existing
DM role continues to control gameplay. The old direct provisioning endpoint is
an operator repair path and is not exposed through the friend UI.

The campaign DM assigns characters with
`PUT /campaigns/{campaign_id}/members/{member_id}/character`: supply either
`{player_character_id}` for an existing character in this campaign or
`{new: {name}}` to create and assign one. A character can belong to only one
player membership. Reassignment replaces the membership link and leaves the
previous character unassigned. Passing `{player_character_id: null}` clears
the link while retaining the character, its sheet versions and its knowledge
grants. Grants follow the character; the former player loses access on the
next request, and a newly assigned player receives that character's grants.

An accepted player without a character uses
`POST /campaigns/{campaign_id}/characters/self` with `{name}` to create and
link their own character. A seated player receives 409; the DM uses the
assignment route. Creation adds no sheet version. The existing draft,
submission and DM approval flow is unchanged. The DM-only members list
includes each assigned character's name. Lobby campaign entries include only
the caller's own `player_character_id` and `character_name`, both null when
unassigned. A session screen remains follow-up work.

**Why.** Character-owned sheets and grants survive seating changes without
moving knowledge between accounts. Campaign-scoped roles let a DM seat their
own table without operator access.

### Optional single-use campaign links (disabled)

`grimoire.invitationLinks.enabled` defaults to false. When enabled, campaign
owners create a seven-day, single-recipient link for a registered player and
copy it themselves. The raw capability is shown once, carried in a URL
fragment, then exchanged for a Secure, HttpOnly, SameSite=Lax resume cookie.
The database stores only its SHA-256 digest. Opening the link does not grant
membership or consume it. An explicit authenticated Join inserts membership
and consumes the link in one transaction. Existing recipients are bound to
immutable app-account IDs, including after email changes. Repeating a successful
Join returns success only for that same identity while membership still exists.
Removing a player revokes their old links; replay cannot restore access.

Campaign locking serializes issuance, redemption, revocation and removal.
There is only one unexpired pending link for a campaign and email. Duplicate
Create requests return an instruction to revoke the old link before replacing
it; they never recover a stored raw token. Links that expire or are revoked
cannot be redeemed. The exact anonymous `/grimoire/join` route serves a
self-contained, no-store landing page with no telemetry or authenticated assets.
The authenticated `/grimoire/join/accept` route and every other Grimoire route
keep their existing OIDC policy. No private API is exposed on the friends host.

New-account enrollment has a second default-off flag,
`grimoire.invitationLinks.enrollmentEnabled`. Issuance requires both campaign
ownership and the existing `operators` account-administration entitlement.
Ordinary campaign owners cannot create account-enrollment invitations. The
backend adapter only creates expiring, single-use invitations for the configured
flow, with a fixed email and a stable account username derived from normalized
email. This username is not a credential. Its database uniqueness prevents
concurrent or replacement enrollment flows in this integration from creating
multiple accounts for one recipient. Authentik's separate optional
`grimoireLinkEnrollment.enabled` flag mounts `grimoire-link-enrollment`; no
existing enrollment, Moving, provider or group policy is modified. The new
flow discards unapproved fixed-data fields and forces external, ungrouped
accounts. New recipients must present this app's verified Grimoire issuer and
the invited email when they Join. The capability authorizes this invitation;
email never links or transfers an existing account's memberships.

The chart creates no service account, API token, RBAC grant or Secret. Enabling
account enrollment requires a separately reviewed existing Operator-managed
Secret reference and the exact flow ID. The credential is mounted only in the
backend. Proposed access is model-wide `add_invitation` plus initial
object-level `view_invitation` and `delete_invitation` for this role's newly
created invitations. Those initial permissions must be verified in an isolated
integration test before provisioning. No global view/delete/change, user,
group, provider or permission-management grants are intended. Importantly,
Authentik's creation permission is not flow-scoped: a stolen credential can
create invitations for other flows with arbitrary fixed data. The app's
allowlist does not eliminate that credential-compromise risk, nor does the
model permission provide a separate prohibition on Authentik email actions
for accessible invitations. The integration never calls those actions. This boundary
needs explicit approval before enablement.

Authentik consumes its single-use token at the invitation stage, before signup
finishes. The outer campaign link stays pending and can obtain a replacement
account invitation after an interrupted signup, with the same fixed username.
If an account already exists, the recipient signs in instead. An upstream
timeout can leave an orphan invitation; it remains bounded by the campaign
expiry and the same unique account identity. Revocation commits locally first,
then attempts Authentik cancellation. Provider failures leave a visible cleanup
retry without restoring campaign access. An already-started Authentik flow can
still finish account creation after its token is deleted; revocation guarantees
no campaign access, not cancellation of an in-progress upstream signup.

App-side HTTP logging and instrumentation exclude enrollment credentials.
Authentik itself has upstream invitation-token debug logging; its logging and
ingress redaction must be reviewed before enabling this integration. Never put
real invitation URLs, provider UUIDs or API tokens in CI logs, PRs or fixtures.

**Why.** Account admission and campaign membership are separate security
boundaries. Keeping a single-use app capability until the final Join provides
atomic membership, safe retries and revocation without treating Authentik's
partially completed enrollment as campaign acceptance.

### Optional-link enablement checks

Human-owned access review and activation are tracked in [#6858](https://github.com/jomcgi-org/homelab/issues/6858).
Keep both flags off until reviewed provisioning and normal CI have passed.
Before enabling, test the exact pinned Authentik version and Envoy routing with
disposable accounts: fresh signup, existing-account sign-in, interrupted signup,
Back/Close/reopen, wrong-account rejection, duplicate submission, expiry,
concurrent redemption, revoke during signup, provider outage and cleanup retry.
Verify the adapter cannot read/delete unrelated invitations, and explicitly
review the remaining model-wide create privilege. Check that the anonymous
route is exact-only and does not expose `/join/accept`, assets or private APIs.
Verify no operator/family group assignment and no Moving-policy changes.

### Deployment and verification

The `k8s-homelab/grimoire-oidc` 1Password item holds a concealed `client-secret`.
The Operator mirrors this item into both Authentik and Monolith namespaces.
Authentik reads it as `GRIMOIRE_OIDC_CLIENT_SECRET`; Envoy reads the same value
from `grimoire-oidc-client`. No secret is stored in Git. Provision the item
before deploying the blueprint and route. Production enables the route; dev
explicitly disables it to avoid claiming the production hostname.

After the normal Linux CI and deployment gates, verify with two fresh accounts:

1. The account invitation link enrolls its recipient; enrollment without a
   token fails. The recipient cannot replace the invitation's email.
2. The new user arrives at an empty Grimoire lobby and can create a campaign.
3. Inviting the second registered user leaves them without campaign access
   until they accept. Declining or cancelling grants no membership.
4. An accepted player can see their campaign but cannot invite or remove
   members. Removal takes effect on the next request.
5. Ordinary users cannot access Authentik administration. The Grimoire lobby
   has no email-delivery option or global account-admin redirect. Optional
   enrollment controls require the existing account-administrator entitlement.
6. The moving app and preview lane retain their original access policies.
   Shared `/_app/` assets accept either app's signed cookie, while page and API
   permissions remain separate. Requests to `/api/grimoire` on the friends
   hostname have no backend route.
