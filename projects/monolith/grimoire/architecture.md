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
10) scored hits with `type` (`entity`, `note`, `event`, `fact`, or `chunk`) and `source`
link fields: `entity_id`, `note_id`, `session_id` plus `seq`, or `book_id` plus
`chunk_id`. Facts carry only `fact_id` as their source, with no lore link.
The public corpus `/search` endpoint remains available.

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
- The public pages are Svelte: `projects/monolith/frontend/src/lib/public/grimoire`
  and `projects/monolith/frontend/src/routes/public/app/grimoire`. The private
  operator sheets page is `projects/monolith/frontend/src/routes/private/grimoire`.
- The friends web app lives in `projects/monolith/web/grimoire`. See
  [Friends web app](#friends-web-app).
- Persistent state lives in the Monolith Postgres cluster. There is no separate
  Grimoire deployment or datastore.

## Data model

### Character facts

`character_fact` stores viewer-owned campaign facts and their evidence event ids.
The validating writer rechecks visible evidence and granted entity vocabulary in
the transaction. Evidence retraction invalidates dependent facts and vectors.
Events are locked in id order before dependent facts; embedding collection and
vector persistence follow the same order. PostgreSQL concurrency tests exercise
the writer, retraction and embedding collection together.

Knowledge search exposes own and party facts to an assigned player, all facts
to the DM, and none to characterless members. It rechecks live status after
vector retrieval.
The search UI renders fact status and offers an own-fact dispute action. The
dispute endpoint returns the same 404 for other-PC, party, foreign-campaign,
retracted and absent facts. A player cannot mutate party facts. Responses carry
no evidence, entity, author or PC identifiers. Disputing is idempotent.

`facts_for_context` re-reads only active facts when assembling extraction input.
`search_knowledge(..., for_context=True)` also excludes disputed rows even when
their vectors remain. The future private campaign assistant must use these
active-only paths. The existing public corpus chat never reads campaign facts.

**Why.** Players need to see disputed facts to correct their knowledge while
assistant context must exclude them. Live source checks apply after retrieval;
an old embedding or cached ORM row cannot restore a disputed fact to context.

Session end queues only campaign, session and viewer identities. Completed queue
names remain replay fences. `grimoire.kg.dailyCap` defaults to zero. Claiming
requires a positive independent cap and a registered tool-free transport. This
release registers none. Previously claimed or replayed Grimoire jobs finish
disabled before prompt assembly, retries or any Ember extraction session.

**Why.** An ordinary Luna agent has shell, web and agents MCP access. A prompt
instruction or empty checkout cannot keep campaign text out of generic knowledge
tools. The tool-free transport choice and live acceptance remain in #6991.
Repository delivery on PR #6958 is staged and default-off; #6627 stays open.

### Shared corpus and play state

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

## Read-aloud voice map

`grimoire.campaign_voice` stores a DM-editable voice preset per speaker key.
Campaign members can read the map. Keys are `narrator`, an NPC entity UUID in
the campaign's corpus, or a free label of up to 64 characters. Presets contain
an optional language hint, up to eight preferred voice-name substrings, a rate
from 0.5 to 2, and a pitch from 0 to 2. Narration events accept an optional
speaker key independently of whether a preset exists.

**Why.** Browser `speechSynthesis` supports read-aloud with zero new
infrastructure. Players receive opaque references for NPC entity keys in both
the voice map and narration bodies, so hidden NPC ids stay private. Free labels
and voice hints are visible to every admitted member: the DM uses an NPC entity
key for a hidden identity and avoids identifying text in hints. DMs retain raw
keys, and the DM map adds `speaker_name` for NPC keys still visible to the DM.
The DM speaker pickers list saved presets from it and find other NPCs by name
search; the session poll requests no NPC list. An NPC key without a name has
left the DM's view: narration omits it, and the preset editor offers only its
deletion, which checks the stored row and no entity. Server-generated audio is
tracked separately in #6629.

The browser queues only events already received in the session feed, in sequence
order with one utterance in flight. Initial history is marked handled without
playback. A new narration delivery cancels earlier queued and current speech;
events in that delivery remain ordered. End and error callbacks advance the
queue. Stop clears it. Retracted events, silent reveal items and individually
retracted entities are skipped. Name-only reveals speak only the granted name.
Presets are resolved by the speaker key exactly as received, including `ref:`
keys. Missing presets fall back to `narrator`, then rate 1 and pitch 1. Preferred
name substrings, exact language and language subtag are tried in that order;
otherwise the device default voice is used. The browser's asynchronous
`voiceschanged` event refreshes the available voice list.

Read DM narration defaults on for the DM device and off for players. The DM
device is the room speaker: it reads table narration only, never private DM or
PC narration and never reveals, even when the DM received them. Players can
opt into table narration and independently into their received reveals and
narration addressed to their own character. Both toggles are stored in local
storage per campaign on each device. To designate a table member's device, the
DM turns narration off on their device and that member turns narration on.
Other player devices remain silent unless those players opt in. Browsers without
speech synthesis hide the playback controls.

`createSpeaker` accepts a `micPauser` with `pause()` and `resume()`. It pauses
before the first utterance of a run and resumes after drain, stop or cancellation.
The queue tests exercise this seam; #6621 remains open and wires its microphone
capture into it. This delivery does not capture audio.

## Visibility and public access

Private DM routes can read the complete corpus. Player-scoped reads centralize
the `is_global OR granted-to-player` rule and apply the grant scope when
projecting details. Public corpus routes are read-only. Full text and page
images fail closed unless the book is explicitly classified as open-licensed;
copyrighted books expose only derived entities, graph structure, and bounded
snippets.

## Transcripts

The transcript routes require `GRIMOIRE_TRANSCRIPT_ENABLED` to be exactly
`true`, read on every request.
`grimoire.transcript.enabled` defaults to false until the capture surface is
ready. No deploy override enables it.

Members grant or revoke their own consent per campaign, including the DM and
members without characters. Consent records the named processor and grant and
revocation times. Repeating an active grant for the same processor keeps the
row; changing processor revokes it and creates a replacement in one transaction.
Players read only their own history, without member ids. DMs read all campaign
consent rows. Grant and revoke serialize on the member row with `FOR NO KEY
UPDATE`, permitting event-author foreign-key checks. Revocation updates the
consent row so it waits for an ingest reader's shared lock.

Sessions start with transcripts off. DMs can set off, on or paused. Players can
pause from on, and a repeated pause is a no-op. Ended sessions refuse all state
writes. Each actual transition locks the session and appends one table-audience
system event with the caller as author, the previous state and the new state.
All members read the current state. Consent and state requests forbid unknown
fields, including audio. No raw audio is accepted or stored by this surface.

Utterance ingest accepts attributed text with timezone-aware start and end
times, confidence and a browser, table or Discord source. Browser speakers
must be the caller. Table and Discord adapters authenticate as the campaign DM
and name a campaign member; a table adapter may instead supply a label for an
unknown speaker. That unknown-speaker path requires consent from every current
campaign member. The server locks the session before share-locking active
consent rows and appending the utterance. Off, paused and ended sessions reject
ingest. Member ids remain outside the event body. Retry ids deduplicate the
same utterance; a changed payload conflicts.

Table talk defaults to all campaign members. DM whispers use the existing
audience contract, including its characterless-member restriction below.
DM-authored PC asides reach the listed characters and DM. Event-list responses
project author ids for the caller; the journal currently does not fold
utterance text. Ingest forbids unknown fields, including raw audio.

Every campaign member can read retention settings. The DM sets
`transcript_retention_days` from 1 to 365 days, with a default of 30. The daily
`grimoire-redact-transcripts` CronWorkflow in `monolith-workflows` runs at 03:32
UTC regardless of the transcript flag. It redacts utterances older than the campaign window by
server `created_at`, preserving a stub with `redacted: true`, timing, source
and speaker label when present. The speaker remains in `author_member_id`.
Event ids, sequence numbers and check-in state stay unchanged. Stubs retain
the table, DM whisper and listed-PC audience rules above, including the
characterless-member restriction. The job deletes each expired utterance's
transcript and legacy event-keyed vectors; the embedder never re-indexes
stubs. Derived summaries, journal entries and facts survive. Raw audio is
never accepted or stored.

**Why.** Consent, off-the-record state and retention are server-owned controls
shared by capture providers. Members can read the retention window before
consenting. Timing stubs preserve the session log without retaining spoken
text. Capture stays default-off until live consent, pause, audience and
retention execution checks are accepted.

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
transaction. The helper flushes; the caller commits or rolls back. Grant
updates and deletes lock matching reveal rows and retract the affected reveal
history. Rows and sequence numbers are retained.

**Why.** A Postgres sequence consumes numbers on rollback and would leave gaps.
Locking the existing session row serializes writers within one session while
other sessions can append independently. Allocation and insertion roll back
together. The unique index also rejects a writer that bypasses serialization.

The play HTTP routes are always on. Campaign members may list sessions and poll events. DMs may append every kind except
`utterance` and `roll`; players may append only `action` with `dm` or `table`
audience. HTTP utterances are refused for every caller because #6618 owns
ingest. Generic event writes refuse `roll` for every caller with 403 because
only the server-side roller may produce trusted roll bodies.
There is no generic event-retraction HTTP endpoint. DMs change reveal history
through the grant update and delete endpoints.

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
These rules leave `audience_predicate` unchanged. There are 74 campaign routes
in the route inventory, including the roller, bulk grants, inventory, voice map,
handout upload and image, initiative, and transcript consent, ingest, and retention.

**Why.** Server-side `secrets.SystemRandom` prevents clients from supplying
results or seeds. A dependency override lets tests use a seeded RNG and assert
the exact stored body without changing production randomness. Characterless
players' table-only rule keeps every accepted roll visible to its roller.

Part of #6610, which owns live audience checks.

### Handouts

A DM posts a `handout` event with body `{title, markdown, entity_id?, image?}`
to the table or to chosen PCs through the ordinary events route. The body is
validated and stored normalised (`handouts.py`), so an identical `request_id`
retry stays idempotent. `image` is `{"source": "chunk", "chunk_id"}` for an
image chunk of an open-licensed book, or `{"source": "upload", "key"}` for an
image the DM uploaded through `POST /campaigns/{id}/handouts/uploads` (5 MiB,
PNG, JPEG, GIF or WebP, sniffed from magic bytes) under
`campaigns/<campaign_id>/handouts/` in the grimoire bucket. Images are served
only by `GET .../sessions/{sid}/events/{event_id}/image`, which applies the
audience predicate, hides retracted handouts from players and answers one 404
for missing and non-recipient alike. For non-DM viewers `entity_id` survives
projection (events list and journal Received) only when that viewer can already
see the entity. A player pins a handout through `POST /notes` with
`from_event_id`; the entity link follows the same visibility rule and never
fails the pin.

**Why.** Clients never receive an S3 key to fetch, so the audience predicate is
the only path to the bytes and a retraction withdraws the image with the
event. Chunk images re-check `book.copyrighted_content` at serve time and fail
closed (copyrighted, unclassified or a missing book), so reclassifying a book
withdraws handouts already posted. The upload key is matched in full against
this campaign's prefix and a uuid4 name, which rules out traversal and other
campaigns' objects without trusting the client's filename or declared type.

The React app calls these routes directly (`web/grimoire/src/api/handouts.ts`).
The composer posts the `handout` event to the events route, with limits mirrored
from `handouts.py` in `src/lib/handout.js`. `uploadHandoutImage` posts the file
to the upload route as multipart under the fixed name `handout`, so the DM's
filename stays on their machine. `handoutImageUrl` builds the event image
route's URL from the campaign, session and event ids, each encoded as one
segment, and the page loads the image from the API. The gateway sets
`Cache-Control: private, no-store` on every routed API response. The upload
response gives the DM's page the storage key, which it sends back in the
handout event. For every other viewer `project_handout_body` in `handouts.py`
reduces the image reference to its `source`, so a recipient's page holds only
the image URL. The DM composer offers uploaded images only; chunk images are
accepted by the API but have no picker yet. The frontend container still sets
`BODY_SIZE_LIMIT` to 6M for the deleted upload proxy; no Grimoire upload passes
through it.

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

The UI ships a props-driven `JournalPanel`
(`web/grimoire/src/journal/JournalPanel.tsx`), used by the campaign journal
screen and by the session screen's Journal tab. While the tab is open it reads
the session journal route for the chosen view (`useJournal` in
`src/api/session.ts`) every thirty seconds and again when a new event arrives.
The session screen holds the Mine/Party choice, so a refresh replaces the data
without resetting it. `onShowEvent` and `onOpenKnowledge` are optional panel
props; the campaign journal screen passes only `onOpenKnowledge`, so its
entries open knowledge and have no link to a story feed.

**Why.** One renderer keeps retraction, name-only and partial-journal handling in
a single place. `journalSections` in `src/api/journal.ts` passes the
audience-filtered API shape through and only fills in missing lists.

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
stores exactly `{"retracted": true, "silent": true}`. Earlier matching reveal
entries are retracted. Grouped rows record `retracted_entity_ids` and set
`retracted_at` once all their entries are retracted; single-entity rows set
`retracted_at` directly.

The DM-only, play-gated bulk grant route accepts 1 to 50 unique entity/PC
pairs sharing one entity or one PC. It validates every item before writes,
inserts all grants, appends one event per distinct PC with
`{"reveals": [<per-entity body>, ...]}` in request order, and commits once.
Failures roll back both grants and events.

On the entity list, DM-only `not_granted_to` means "hide entities that PC
already knows": exclude global entities and any grant scope using a
correlated grant alias in the shared visibility query. Knowledge search does
not accept this filter. Authorization checks membership, DM role, then PC
campaign membership, in that order. Live audience checks remain in #6610.

**Why.** The grantee projection bounds the stored feed snapshot. One
transaction keeps knowledge and feed changes together; an identity-free
silent retraction leaves no entity data for a read path to expose. Sharing
the visibility predicate keeps reveal search aligned with player knowledge.

## Inventory

`inventory_item` holds the party pool and each character's inventory without
computing encumbrance. The DM can give, edit, move and soft-delete any item.
Players can change their own quantities and move non-hidden items between their
own inventory and the visible party pool. Characterless members see no items
and cannot mutate any. Deleted items disappear from lists; the DM retains their
audit history. SQL and Python predicates share SQLite and PostgreSQL agreement
tests against an independent oracle. Entity chips and audit entity edits are
projected through each viewer's grants, never exposed as raw hidden entity ids.

**Why.** Players see only their own character's identity, so every character
item, hidden or not, is visible only to its owner and the DM. Non-hidden party
items are visible to every member with a character; hidden party items only to
the DM. Every mutation writes an append-only `inventory_change` with actor,
quantity delta, reason and field/owner provenance. Owner ids in player audit
views become `party`, `you` or `character`. PostgreSQL rejects direct audit
updates and deletes, and direct hard-deletes of items; only nested FK
deletion actions may null the three provenance FKs, and only nested FK
cascades may delete audit rows or items. Campaign and character cascades keep
working while direct deletes fail. This preserves the required
FK lifecycle without allowing edits to the audit contents. A partial move
writes source and destination changes in the same transaction. Inventory is
campaign state and works with play disabled. When play is enabled and a session
is active or paused, every change records its session; non-hidden changes also
append one table `system` event, atomically. Hiding emits no event; unhiding does.
Event text uses only the party pool or a character's inventory, never character,
member or entity identity. With no current session both provenance FKs are NULL.
There is no `public_reader` grant. The sheet engine, not inventory, owns derived
encumbrance.

The play-enabled session screen has an Inventory tab after Notes. Players see
the party pool and their own items, with quantity edits and partial moves.
Hidden owned items retain a text badge and quantity edits; the backend refuses
player moves of hidden items. The DM sees sections named for every campaign PC
and can give, edit, move or delete items. Each item has an on-demand audit
history disclosure. The tab calls the inventory routes directly
(`web/grimoire/src/api/inventory.ts`). The item list refreshes every four
seconds and an item's changes load when its history opens. Give, edit, move
and delete call `POST /inventory`, `PATCH /inventory/{item_id}`,
`POST /inventory/{item_id}/move` and `DELETE /inventory/{item_id}` under the
campaign. A player's patch carries only quantity and reason; the DM's adds
name, notes and the hidden flag.

DM-only Give item buttons on visible, non-retracted reveal entries and handouts
open the tab with the name and source event prefilled. Reveals also carry their
entity id and select the single addressed PC, or the party pool otherwise.
Handouts use their first line and default to the party pool. Knowledge chips
open the existing drawer. Feed system events read as Table update; handout
events read as Handout.

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

Membership and the current character determine the viewer. Players with a character may create
either kind; DMs may create party notes only. Character notes may be edited or
deleted only by their author; party notes by their author or the DM. Only a
character note's author may change its DM sharing flag. The stored campaign
default `notes_dm_readable_default`, initially false, applies only when
creation omits the flag. Explicit false remains private. Invisible ids
return the same 404 as random ids. Search escapes LIKE wildcards and applies
visibility and substring matching in SQL before ordering and limiting rows.

Entity links are checked against the author's grant overlay at write time and
resolved again for each viewer at read time. Chips expose only id, name and
type, dropping invisible entities entirely. Event ids are opaque UUID strings,
stored without a foreign key or event lookup. Author and character ids go only
to the author and DM; the sharing flag goes only to the author. A viewer-computed
`can_edit` flag exposes edit capability without adding author identity to another
player's projection. There is no `public_reader` grant on notes.

The campaign notes screen (`/grimoire/campaigns/{campaign_id}/notes`) and the
session screen's Notes tab call the notes routes directly
(`web/grimoire/src/api/notes.ts`). A failed read shows the failure rather than
an empty list. Mine and Party tabs send `kind` and `q`. Quick-add offers
Campaign default (omits `dm_readable`), Private and Share with DM. DMs see
Shared with you and can only add party notes. Note text renders as plain text
(`src/notes/NoteText.tsx`); entity chips come only from resolved viewer-visible
objects. The feed's pin action posts `from_event_id` to the notes route.

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
standalone authentication, Gemini Live voice pipeline, and standalone React
frontend are not supported architecture. New Grimoire work belongs in the
Monolith domain, its web UIs, or its batch jobs.

## Friends web app

The friends UI at `https://friends.jomcgi.dev/grimoire` is a client-only React
app on the [Astryx](https://github.com/facebook/astryx) component library
(`projects/monolith/web/grimoire`, #7698). The SvelteKit friends pages and
their server helpers, which relayed the ID token to the API, are deleted.
Public Grimoire pages and the private operator sheets page stay in Svelte.

- **Static files.** `grimoire/web.py` serves the built bundle from the API
  port at `/grimoire`. Any path under the base that is not a built asset
  answers with `index.html`; a missing asset is a 404, never the shell. The
  base path is read from the bundle, so server and bundle cannot disagree.
  There is no server rendering, no React Server Components and no server
  actions.
- **Direct API calls.** The browser calls the API itself. The `-grimoire`
  HTTPRoute (`chart/templates/httproute-grimoire.yaml`) forwards each prefix
  in `cfIngress.grimoire.apiPrefixes` to the API port. The list is
  `BROWSER_API_PREFIXES` in `grimoire/browser_lane.py`: the four Grimoire
  prefixes defined there and the four account-API prefixes in
  `auth/browser_cookie.py`. `/api/grimoire` and `/api/auth/platform` are never
  routed whole. `/api/grimoire/books` and `/api/grimoire/chunks/*/image` have
  no identity check of their own, because the private lane's Cloudflare Access
  session is their gate. The account API's enrollment routes are capability
  callbacks with no identity dependency.
- **Credential.** Envoy's OIDC filter decrypts the session cookie before
  forwarding, so the API port receives the signed ID token in the
  `grimoire-id-token` cookie. `browser_token` in `auth/browser_cookie.py`
  reads it for both APIs. `grimoire/access.py` verifies it against the
  Grimoire issuer, audience, signature and expiry;
  `auth/platform/identity.py` passes it to its login verifier. The cookie is a
  credential only when `GRIMOIRE_BROWSER_ORIGIN` is set, which the chart does
  only when the route is enabled.
- **Cross-site requests.** A cookie rides along on requests another site
  makes. A write carrying the cookie must send exactly one `Origin` header
  equal to `GRIMOIRE_BROWSER_ORIGIN`, and `Sec-Fetch-Site`, when present,
  must be `same-origin`. Reads are not restricted: they change nothing and a
  cross-origin page cannot read the answer.
- **Invitation links.** `grimoire/join_landing.py` serves the anonymous
  landing at `/grimoire/join` and the signed-in endpoints
  `POST /grimoire/join/accept/confirm` and `GET /grimoire/join/accept/state`. The
  landing embeds `web/grimoire/src/lib/join-landing.js` as its script. The
  signed-in accept page is the React screen at `/grimoire/join/accept`. See
  [Single-use campaign links](#single-use-campaign-links).
- **Account pages.** The React screens at `/grimoire/platform` and
  `/grimoire/platform/profile` call the account API under
  `/api/auth/platform` (`src/api/platform.ts`).
- **Caching.** Hashed assets are `private, max-age=31536000, immutable`.
  `index.html` is `private, no-cache`, so a deploy takes effect on the next
  reload. API responses are `private, no-store`. Everything carries
  `Cloudflare-CDN-Cache-Control: no-store`: the route's SecurityPolicy gates
  every byte, so a shared cache must never hold a copy.
- **Content-Security-Policy.** The shell allows scripts and styles from its
  own origin only, with one exception: the component library injects a single
  fixed stylesheet for overlay positioning, allowed by its SHA-256 hash. The
  bundle ships the hash in `style-hashes.json` and `src/csp.test.tsx` fails
  when an upgrade changes it.
- **Contract.** `grimoire/browser_api_schema.py` generates
  `web/grimoire/src/api/schema.ts` from the Grimoire response models. A test
  compares the committed file with the generator's output, and the app's
  typecheck compiles against it.
- **Polling.** There is no aggregate state endpoint. The session screen
  assembles independent reads (`src/screens/session/useTable.ts`,
  `src/api/session.ts`). While the tab is visible and the session has not
  ended, it fetches events after the last sequence number it holds every two
  seconds and re-reads the whole feed every fifteen, because a retraction
  edits an old event in place. The lobby, the session list, the voice map and
  the turn order refresh every fifteen seconds; approved sheets every sixty.
  A new event refreshes the journal, and a `turn` event refreshes the turn
  order. Inventory, notes, voices, initiative, grants and reveals call their
  own API routes.
- **Sign-in expiry.** `src/app/signIn.ts` and `src/lib/session-expiry.js`
  handle a lapsed sign-in. See
  [Deployment and verification](#deployment-and-verification).

**Why.** Agents write most of this UI. A component library with tested
overlays, comboboxes, focus handling and announcements removes the parts they
most often get subtly wrong, and one typed contract makes a backend change
that breaks the UI fail in CI. Static files with direct API calls remove a
Node process and a hand-maintained relay layer from the path between a player
and the table.

**Tested.** `grimoire/browser_lane_test.py` covers the cookie credential, the
cross-site rules and an audit that every routed path depends on the Grimoire
identity check while every route without one stays unrouted.
`auth/platform/browser_identity_test.py` covers the same cookie rules for the
account API and audits that only the capability enrollment routes skip the
identity dependency. `chart/grimoire_browser_lane_test.py` pins the rendered
route to the prefix list, to the one SecurityPolicy and to the exact anonymous
landing path. `grimoire/web_test.py` covers the shell, the asset fallback and
the response headers. `grimoire/join_landing_test.py` and
`web/grimoire/src/lib/join-landing.test.js` cover the invitation landing.

**Outstanding.** The `X-Grimoire-Token` and `x-platform-token` header paths
remain in `grimoire/access.py` and `auth/platform/identity.py`. Tests and
`dev/grimoire/rehearse_join_links.py` send them. No production component
originates either header; the private sheets loader
(`frontend/src/routes/private/grimoire/+page.server.js`) copies
`x-grimoire-token` only when an incoming request carries it. Live verification
of the invitation flow and the account pages against the production gateway is
outstanding (#7698). The browser rehearsal harness in
`projects/monolith/dev/grimoire` drives the React app.

### Feedback

Every page header has a Feedback action. `POST /api/grimoire/feedback` stores
the text (up to 2,000 characters), the in-app path, the campaign when the path
has one, and the sender's verified email in `grimoire.feedback`. A campaign
the sender does not belong to is dropped from the row. Each sender may
store ten rows in any 24 hours; the next request answers 429. An advisory
transaction lock per sender serializes the count and the insert through commit.
The count takes a fresh READ COMMITTED snapshot after lock acquisition. A
pathname cannot carry a host, query, fragment, backslash or control character.

Only operators read the full rows, at `/grimoire/feedback`. The routes apply
the lobby's `can_administer_accounts` rule (`operators` group) on every request.
The lobby carries `new_feedback_count` for operators and zero for everyone
else.

An operator dismisses an item, with an optional one-line reason of up to 200
characters, or files it. Filing creates one issue in
`GITHUB_REPO` through `core.github.create_issue`, holding only the title and
body the operator typed, the footer `Filed from Grimoire feedback.` and the
labels `grimoire` and `feedback`. The issue carries no readiness label, so the
factory's refine step picks it up. A GitHub failure answers 502 and leaves the
row `new`; a row that is already filed or dismissed answers 409. The row keeps
the operator's title and starts with `issue_state = 'open'`.

#### Roadmap

Every signed-in user reads `GET /api/grimoire/feedback/roadmap` at
`/grimoire/roadmap`: the 200 newest visible items, each in one of four states.

| State       | Meaning                                                      |
| ----------- | ------------------------------------------------------------ |
| `new`       | Sent, not yet triaged.                                       |
| `accepted`  | Filed, and its issue is open.                                |
| `completed` | Its issue was closed as completed.                           |
| `rejected`  | Dismissed, or its issue was closed as not planned or duplicate. |

A note sent after `20261113090000_grimoire_feedback_roadmap.sql` is `shared`:
everyone sees its text. A filed item shows the operator's title instead. Each
item carries its vote count, whether the caller voted, whether the caller wrote
it, its issue link once filed, and the dismiss reason once rejected.

These stay private: the author, the page and the campaign of every note, and
the text of notes sent before the migration (`shared = false`). An unshared
note appears on its author's roadmap only. Once filed it appears for everyone
under the operator's title, or as "Suggestion" when it was filed before titles
were stored. The query applies this rule; the browser receives no hidden rows.

GitHub is the source of truth after filing. A roadmap read refreshes
`issue_state` when any filed row has an `issue_checked_at` that is null or
older than ten minutes. `core.github.list_issue_states` makes one request for
the 100 most recently updated issues labelled `grimoire` and `feedback`, with
the eight-second deadline and one-megabyte cap of `create_issue`. A request
takes `pg_try_advisory_xact_lock` first; a request that does not get the lock
serves the stored states. A failed read is logged by exception type, stamped
like a successful one, and the stored states are served. An issue missing from
the answer keeps its stored state.

`PUT` and `DELETE /api/grimoire/feedback/{id}/vote` record or withdraw one
"Me too" per user in `grimoire.feedback_vote`. Both are idempotent and answer
with the new count. Votes are open while an item is `new` or `accepted`; a
resolved item or the caller's own answers 409, and an item the caller cannot
see answers 404. The operator inbox shows the count.

**Why.** Friends kept sending suggestions someone else had already sent, and
nobody heard what became of theirs. The roadmap shows both, so a friend checks
it before writing and adds a vote to an existing item. Notes sent before the
roadmap were written to be read by an operator only, so they are never shown to
anyone else. The author's name is left off every item so a suggestion is judged
on what it says. Authors cannot vote for their own item, so a count means other
people want it too. Reading issue states on the roadmap request needs no
scheduled job; the ten-minute throttle and the try-lock keep it to one bounded
GitHub call per interval however many people are reading.

`jomcgi-org/homelab` is public. A friend's raw words can name people
or a campaign, and the factory reads the issue, so nothing from the row is
published: the dialog shows the private text separately for reference with an
initially empty, required public body. This staged repository delivery explicitly
rescopes the original prefilled-body detail to preserve the privacy boundary.
Operational acceptance remains outstanding. The server sends only what the
operator typed. The GitHub call runs before the row is marked, under a row lock,
so two operators cannot file one item twice. It is a single attempt with no
retry, because retrying a write whose answer was lost would file a duplicate.
A lost response or a database failure after publication returns a 502 warning
to reconcile GitHub before filing again. The GitHub request has an eight-second
overall deadline, including streamed response reads, inside the browser's
15-second budget. If the browser times out, loses the connection or receives
an unreadable gateway response, filing shows the same reconciliation warning.
Neither path retries automatically.

**Tested.** `grimoire/feedback_test.py` asserts on the captured GitHub payload
and covers the rate limit, the operator rule and the failure paths. It also
covers the roadmap's visibility rule and exact field set, votes, dismiss
reasons, the state mapping, the refresh throttle and a GitHub outage.
`core/github_test.py` covers `create_issue` and `list_issue_states`.
`grimoire/feedback_postgres_test.py` uses the ephemeral PostgreSQL migration
harness and separate connections to cover simultaneous quota requests, the
rolling boundary, sender isolation, concurrent resolution and public-reader
privileges. The concurrency tests pause after count or row locking and inspect
PostgreSQL's waiters before releasing the first transaction. It also holds one
roadmap request inside its GitHub read while a second serves stored states
without calling GitHub, and checks the new column constraints and the vote
cascade.

## Friend onboarding and campaign ownership

The friend UI lives at `https://friends.jomcgi.dev/grimoire`. Its dedicated
Authentik provider and OIDC cookies are separate from the operator and moving
applications. The React app presents the signed Grimoire ID token in its
session cookie, which the backend independently checks against the Grimoire
issuer, audience, signature and expiry. The browser reaches only the API
prefixes listed under [Friends web app](#friends-web-app). This provider is not added to the shared
operator/MCP token resolver.

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

With platform enforcement off, registered users can create campaigns unless the
staged local creation restriction is enabled. Creators become owner and initial
DM. Owners invite registered players by exact email, without a user directory.
Recipients must accept in their lobby before any campaign data becomes
available. Decline, cancellation and membership revocation are durable;
re-inviting creates a fresh invitation ID so an old acceptance cannot be
replayed. Ownership controls invitations and membership removal; the existing
DM role continues to control gameplay. Membership admission uses accepted
invitations or campaign join links; the direct provisioning endpoint is removed.

### Staged campaign creation policy

`grimoire.campaignCreation.restrictionEnabled` defaults to false in the chart;
no deployment overlay enables it. It supplies
`GRIMOIRE_CREATION_RESTRICTION_ENABLED`. When true and platform enforcement is
off, `grimoire.campaignCreation.allowedEmails` (default `[]`, supplied as the
comma-separated `GRIMOIRE_CREATOR_EMAILS`) restricts creation to verified
mailboxes. Both configuration entries and signed emails are stripped and
lowercased. An empty allowlist denies every creator. No live identity is seeded.
Mailbox proof is `email_verified` or the configured trusted Cloudflare Access
issuer, as for legacy account linking. Request fields, proxy email projections,
campaign ownership, membership and roles cannot grant creation rights.

The mutation dependency and lobby `can_create_game` use one policy. The existing
frontend capability handling hides creation when denied. With global platform
enforcement on, `grimoire.create_game` remains required regardless of the local
switch or allowlist; the local list never substitutes for a platform grant.
SQL visibility and audience predicates, admin invitations and registered-player
single-recipient links are unchanged. A recipient must still sign in before the
DM can link them.

**Why.** A local allowlist bounds creation without enabling platform enrollment
or duplicating the SQL visibility rules. The active platform permission check
stays authoritative when global enforcement is on.

This is repository-only staging for #7461. Activation and creator selection need
a fresh decision from Joe. Authentik, credentials, Discord registration,
1Password provisioning, source enablement and disposable-account tests remain
human-owned. Operational acceptance remains on #7461. Within 72 hours after
separately authorized landing, the delivery owner must record the CI-published
monolith chart version containing this change and a `verify_deployment` result
of `verified`, including its live revision and checks. That rollout check does
not satisfy the human-owned activation checklist.

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
unassigned.

**Why.** Character-owned sheets and grants survive seating changes without
moving knowledge between accounts. Campaign-scoped roles let a DM seat their
own table without operator access.

### Single-use campaign links

Campaign owners create a seven-day, single-recipient link for a registered player and
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
self-contained, no-store landing page with no telemetry or authenticated assets
(`grimoire/join_landing.py`, with its script in
`web/grimoire/src/lib/join-landing.js`). The React accept screen at
`/grimoire/join/accept`, its endpoints `POST /grimoire/join/accept/confirm` and
`GET /grimoire/join/accept/state`, and every other Grimoire route keep the OIDC
policy. The friends host routes only the API prefixes listed under
[Friends web app](#friends-web-app).

New-account enrollment has a default-off flag,
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

### Discord sign-in

The Authentik chart's `grimoireDiscordSource.enabled` flag, on in the hub
overlay since 2026-11-12, adds
a Discord OAuth source and a Grimoire-only sign-in flow so a friend can enrol
by signing in with Discord instead of receiving an administrator-made
invitation. The source matches by Discord account id, never by email. The
enrollment gate requires a Discord-verified email that no existing Authentik
account uses, and creates an external user under the `grimoire` path with no
group. First login then creates the Grimoire account as today. Campaign
admission stays with invitations and campaign links. Campaign creation is not
gated while platform enforcement is off, so any enrolled Discord account can
create campaigns; Joe accepted that when enabling. The credentials come from
the shared Bosun Discord application. Follow-up lives in
[#7461](https://github.com/jomcgi-org/homelab/issues/7461).

### Link enrollment enablement checks

Human-owned access review and activation are tracked in [#6858](https://github.com/jomcgi-org/homelab/issues/6858).
Keep enrollment off until reviewed provisioning and normal CI have passed.
Before enabling, test the exact pinned Authentik version and Envoy routing with
disposable accounts: fresh signup, existing-account sign-in, interrupted signup,
Back/Close/reopen, wrong-account rejection, duplicate submission, expiry,
concurrent redemption, revoke during signup, provider outage and cleanup retry.
Verify the adapter cannot read/delete unrelated invitations, and explicitly
review the remaining model-wide create privilege. Check that the anonymous
route is exact-only and does not expose `/grimoire/join/accept`,
`/grimoire/join/accept/state`, `/grimoire/assets` or any API prefix.
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
   Shared `/_app/` requests redirect only to the moving lane
   (`chart/templates/httproute-friends-assets.yaml`); Grimoire assets load from
   `/grimoire/assets` on the Grimoire route. Page and API permissions remain
   separate. Under `/api/grimoire` and `/api/auth/platform` on the friends
   hostname, only the prefixes in `cfIngress.grimoire.apiPrefixes` have a
   backend route: `/api/grimoire/books` and any other unlisted path answer 404.
7. A single-use link opens the anonymous landing at `/grimoire/join`, and the
   signed-in recipient joins from `/grimoire/join/accept`. `/grimoire/platform`
   and `/grimoire/platform/profile` load for a signed-in account.

Sign-in renews in place. The gateway requests `offline_access`, which the
provider maps, so Authentik issues a refresh token and Envoy renews the
one-hour tokens on the first request after they expire. The provider rotates a
refresh token only in its last day, so two requests renewing together do not
revoke each other. With refresh tokens, ending a friend's Authentik session or
editing the application's policies no longer signs them out of Grimoire; to
cut someone off, deactivate their Authentik account, which deletes their
refresh tokens, or remove them from the campaign. When renewal fails, a fetch
(`Sec-Fetch-Mode: cors`) gets a 401 and a navigation still redirects to the
login. `web/grimoire/src/api/client.ts` sends every request with
`redirect: "manual"` and reports a 401 or a login redirect to
`src/app/signIn.ts`. A single one can be transient, so the session screen
shows Reconnecting and `src/lib/session-expiry.js` treats it as expiry only
when another arrives two to thirty seconds later with no other answer between
them. Then, once no send is in flight, the session composer keeps its text,
its audience, any interrupted send's request id and the viewer's user id and
role in the tab's `sessionStorage`, and the tab reloads at most once a minute;
within that minute it shows a signed-out notice. Other screens show the
signed-out notice and leave the reload to the player. A restored draft never
widens its audience, and a draft kept for another viewer is dropped. A poll
that answers for a different account, role or character reloads the session
screen, because panels such as knowledge search keep their own state. The live
check remains: hold a real login past the access-token lifetime and confirm
the session stays Live without a reload.

## Staged server read-aloud (#6629)

`grimoire.tts.enabled` and `grimoire.tts.cacheEnabled` are false in chart
and deployment values, including GKE. They feed `GRIMOIRE_TTS_ENABLED` and
`GRIMOIRE_TTS_CACHE_ENABLED`; the latter is forced false while speech is off.
The API gate returns 404 before reading the request body or initializing a
runtime or object-store client. Disabled renders contain no synthesis container,
temporary volume or retention job. Nothing in this change enables the service.

The staged CPU runtime is eSpeak NG 1.52.0-r0 from Alpine 3.22 community.
`grimoire/tts_runtime/apko.yaml` and its generated lock pin the Linux amd64
package, dependencies and their checksums. Bazel packages the standard-library
worker into that apko image. eSpeak's bundled English formant data is the model:
`espeak-ng-1.52.0-r0:en-gb:v1`. There is no model download, hosted TTS API or
new secret. Upstream source and model data are at
<https://github.com/espeak-ng/espeak-ng/tree/1.52.0>, GPL-3.0-or-later;
Alpine's package metadata records the same license. Upstream COPYING provides the license text.
Source corresponding to the package is available through Alpine's
aports repository, community/espeak-ng, commit
`e5f14c0a087c0fbd2877700307955e6153f805af`.

**Why.** Formant synthesis runs on CPU with small memory needs and packaged
voice data. It sounds more synthetic than neural narration. That tradeoff
keeps this repository delivery reproducible without new downloads or services.

When separately enabled, the pod gains a non-root, read-only-root-filesystem
sidecar listening only on `127.0.0.1:8094`, with no Service port or credential
environment. It requests 100m CPU and 64 MiB and limits itself to one CPU and
128 MiB. An 8 MiB memory-backed `/tmp` bounds transient output. One HTTP request
and one subprocess run at a time, with an eight-second process timeout and
killing/reaping on timeout. The API allows one in-flight synthesis per process,
returns 429 rather than queueing, and applies a ten-second loopback HTTP timeout.
The browser allows twelve seconds across creation and retrieval before falling
back. Text is capped at 2,000 characters, request JSON at 256 bytes and WAV at
2 MiB (base64 JSON delivery adds one third). Plain stdin is used without SSML,
client-controlled paths or executable voice names.

Both POST `/campaigns/{campaign_id}/tts` and GET
`/campaigns/{campaign_id}/tts/{event_id}/{clip_id}` require the existing browser
identity dependency and current campaign DM membership. Only unretracted table
narration in a session belonging to that campaign qualifies. The server derives
text and speaker from that stored event before accessing inference or a cache.
GET rechecks those conditions on every request, including cache hits, and checks
the current synthesis identity before reading audio. Responses are private and
no-store. Bucket objects remain private; no public or presigned URLs exist.
Opaque SHA-256 clip names contain no NPC identifiers. A revoked membership,
changed role, private audience, retraction or changed preset denies a replay.

Each API process permits six speech requests per member per rolling minute,
including retrievals; its identity table is capped at 1,024 active members.
The GKE overlay has two replicas, so the aggregate bound is twelve requests
per member per minute and two simultaneous syntheses. This assumes the existing
single API process per pod. Scale/worker changes require revisiting these limits.
The optional private R2 cache shares clips between replicas. With caching off,
a retrieval routed to a different replica may miss its local cache; the browser
falls back. A cache outage has the same behavior. This is a future enablement
tradeoff to validate with the deployed routing and replica count.

Speaker keys use the existing campaign map. `narrator` selects `en-gb`; an
available NPC UUID or free label with a saved preset deterministically hashes
its canonical campaign/key pair into `en-gb`, `en-gb+m1`, `en-gb+f2`,
`en-gb+m3` or `en-gb+f4`. Missing presets and deleted/unavailable NPCs use the
narrator preset, then rate/pitch 1. Server voices use the preset's rate and
pitch; browser language/name hints remain browser hints. The cache identity
includes canonical campaign, exact stored text, selected voice, rate, pitch,
preset key/hints/update timestamp and model/version. A preset or model change
invalidates the old clip. Browser payloads retain existing opaque player refs.

The process cache is bounded to 32 MiB. Optional R2 storage lazily uses the
existing HTTPS S3 endpoint, bucket and Operator-provided credentials, with
bounded reads and no retries. It creates no bucket, policy or secret. Keys live
under `tts/v1/`; metadata sets expiry seven days after synthesis. Expired
objects are never served. `R2Cache.retention_page` stages a bounded 100-object
sweep for objects last modified at least seven days ago, with a continuation
token. No scheduler, endpoint, startup hook or delivered workload calls it.
Physical deletion therefore remains outstanding until separately authorized
retention execution exists. Tests mock all storage and deletion operations.

The DM device tries server audio for fresh table narration through the existing
queue and typed API module. Players keep browser synthesis and their separate
opt-ins for table and own-private narration/reveals. Initial history stays
silent. Stop, unmount, campaign/identity change and newer narration abort pending
requests, pause audio and fence late responses. Microphone pause spans loading
and playback, then resumes on drain or stop. Disabled, failed or timed-out
requests fall back to browser speech once. An audio failure after playback has
started advances without repeating already spoken text. Existing reveal
projection, player privacy and microphone implementation remain unchanged.

Before enablement, the authorized owner must review resource behavior, voice
quality, per-replica limits, routing/cache misses and private storage access,
then authorize any cache/retention execution separately. Full enabled-service
latency, playback, cache and retention acceptance remains outstanding on #6629.
Repository tests and chart renders provide no live acceptance evidence. After
landing, the owner must obtain `verify_deployment` success for the delivered
revision and read configuration/workload status to confirm speech remains off
and no inference workload or retention job is active. These staged-rollout
checks do not close #6629.
