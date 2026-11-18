# Grimoire on the factory cluster

[HOME-257](https://factory-plan.jomcgi.dev/factory/issue/HOME-257) plans the factory migration of the [current hub implementation](../monolith/grimoire/architecture.md). Keep existing bookmarks through path-specific Worker redirects. HOME-261 can take the catch-all after these routes move.

## Page ownership

Paths in the first table are relative to `jomcgi.dev/app/grimoire`. Preserve path parameters and reader anchors across redirects.

| Current page or handler | Destination |
| --- | --- |
| / | Static R2 landing in the Astryx site. Publish original pitch and navigation only. Review existing story assets before reuse. |
| /library | Live library on grimoire.jomcgi.dev behind Turnstile. |
| /book/[book], /book/[book]/read, /book/[book]/c/[chunk] | Live gated reader, read handler and chunk permalink. |
| /world, /entities, /entity/[id], /adventure/[id], /explore | Live gated corpus views, including graph, lists and detail responses. |
| /api/[...path] | Drop generic proxy. Move clients to same-origin, explicitly routed gated reads. |
| /chat | Static R2 replay. |
| /chat/s/[id] | Drop arbitrary shares (410). Never redirect to a stored transcript. |
| /chat/session, /chat/message, /chat/share, /chat/fork | Drop public chat handlers. |

The private operator sheet at `private.jomcgi.dev/grimoire` moves to a live Access-protected operator route. On the friends host, move the following routes under `/grimoire` to the live subdomain with their existing roles. Route the anonymous join landing before the authenticated SPA fallback. Preserve invitation fragments on legacy links.

| Live friends routes relative to /grimoire | Function |
| --- | --- |
| /, /sheets, /feedback, /roadmap | Lobby, sheets and feedback |
| /campaigns/:campaignId/session | Session events, dice, initiative, handouts, inventory and transcripts |
| /campaigns/:campaignId/notes, /campaigns/:campaignId/journal, /campaigns/:campaignId/grants, /campaigns/:campaignId/entities/:entityId | Campaign-scoped reads and writes |
| /join (GET, POST), /join/accept, /join/accept/confirm (POST), /join/accept/state (GET) | Invitation and enrollment |
| /platform, /platform/profile | OIDC account and profile |
| /* | Existing unknown-route screen and asset 404 behavior |

## Services and boundaries

Deploy the friends React/TypeScript bundle, operator and corpus Svelte views, and Python FastAPI handlers on factory. Give the corpus read service a separate upstream and read-only database role. Verify Turnstile server-side and issue a short-lived host-scoped admission cookie. Preserve the public router's book-license checks after admission. Copyrighted verbatim text and scans still return 403 on reader, chunk and image requests. Require admission on every corpus response, including direct JSON, reader fragments, searches and image requests. Disable shared caching there. Serve images by chunk ID through the gated Python image handler using a private S3-compatible bucket. Resolve keys server-side and generate needed derivatives into the same private store. Replace hub /img links before switching readers.

Keep operator APIs behind Cloudflare Access. Preserve the friends gateway's explicit browser_lane.py and auth/browser_cookie.py path allowlists, OIDC issuer/audience validation, same-origin write checks, campaign roles and row-level audience checks. Migrate account enrollment, identity mapping and invitation callbacks with their existing providers. Keep enrollment capabilities single-use. Turnstile grants corpus browsing only.

Use factory Postgres with pgvector for book chunks, entity and play embeddings alongside campaign records. Carry forward dimensions, model identifiers, vector indexes and audience predicates. Run ingestion, extraction and play-embedding jobs as private Python workers with separate service accounts. Configure an internal OpenAI-compatible inference endpoint for authorized server-side jobs and private chat with retrieval scoped to the caller. Private chat requires identity and campaign authorization before retrieval. Disable anonymous session creation. Keep model credentials server-side.

For the public replay, record an operator-scripted conversation in an isolated test campaign with original or explicitly redistributable fixtures and no production retrieval. Export only reviewed speaker labels, display text and relative playback timings. Exclude tool traces, prompts, citations to private sources, identifiers and user sessions. Publish the approved versioned file and page via the HOME-137 cluster-to-R2 sink. Fetch the file on Play as in HOME-198. The page needs no microphone, prompt input or inference endpoint. Give the publisher access only to approved artifacts. Book text, scans, embeddings, derived lore, graph data, summaries and campaign material must never enter public R2, build output, search indexes or replay assets.

## API disposition

Each block lists routes relative to its prefix. Identical paths on different upstreams retain distinct admission policies.

### Live gated corpus under `/api/grimoire`

```text
GET /adventures
GET /adventures/{adventure_id}
GET /books
GET /books/{book_id}/adventures
GET /books/{book_id}/read
GET /books/{book_id}/sections
GET /chunks/{chunk_id}
GET /chunks/{chunk_id}/image
GET /entities
GET /entities/{entity_id}
GET /entities/{entity_id}/mentions
GET /entities/{entity_id}/relationships
GET /explore/ego
GET /search
```

### Live campaign and operator API under `/api/grimoire`

```text
GET /alias-candidates
POST /alias-candidates/scan
POST /alias-candidates/{candidate_id}/approve
POST /alias-candidates/{candidate_id}/execute
POST /alias-candidates/{candidate_id}/reject
POST /alias-candidates/{candidate_id}/reopen
GET /books
GET /books/{book_id}/read
GET /books/{book_id}/sections
POST, GET /campaigns
GET /campaigns/{campaign_id}
POST, GET /campaigns/{campaign_id}/characters
POST /campaigns/{campaign_id}/characters/self
GET /campaigns/{campaign_id}/characters/{player_character_id}/sheets
POST /campaigns/{campaign_id}/characters/{player_character_id}/sheets/drafts
GET /campaigns/{campaign_id}/characters/{player_character_id}/sheets/options
PATCH /campaigns/{campaign_id}/characters/{player_character_id}/sheets/{sheet_version_id}
POST /campaigns/{campaign_id}/characters/{player_character_id}/sheets/{sheet_version_id}/approve
POST /campaigns/{campaign_id}/characters/{player_character_id}/sheets/{sheet_version_id}/return
POST /campaigns/{campaign_id}/characters/{player_character_id}/sheets/{sheet_version_id}/submit
GET /campaigns/{campaign_id}/entities
GET /campaigns/{campaign_id}/entities/{entity_id}
POST /campaigns/{campaign_id}/facts/{fact_id}/dispute
GET /campaigns/{campaign_id}/grants
POST /campaigns/{campaign_id}/grants/bulk
POST /campaigns/{campaign_id}/grants/preview
PATCH, DELETE /campaigns/{campaign_id}/grants/{grant_id}
POST /campaigns/{campaign_id}/handouts/uploads
GET, POST /campaigns/{campaign_id}/inventory
GET /campaigns/{campaign_id}/inventory/changes
PATCH, DELETE /campaigns/{campaign_id}/inventory/{item_id}
POST /campaigns/{campaign_id}/inventory/{item_id}/move
POST, GET /campaigns/{campaign_id}/invitations
DELETE /campaigns/{campaign_id}/invitations/{invitation_id}
POST, GET /campaigns/{campaign_id}/join-links
DELETE /campaigns/{campaign_id}/join-links/{link_id}
GET /campaigns/{campaign_id}/journal
GET /campaigns/{campaign_id}/knowledge/search
GET /campaigns/{campaign_id}/members
DELETE /campaigns/{campaign_id}/members/{member_id}
PUT /campaigns/{campaign_id}/members/{member_id}/character
GET, POST /campaigns/{campaign_id}/notes
GET, PATCH, DELETE /campaigns/{campaign_id}/notes/{note_id}
GET, POST /campaigns/{campaign_id}/sessions
PATCH /campaigns/{campaign_id}/sessions/{session_id}
POST, GET /campaigns/{campaign_id}/sessions/{session_id}/events
GET /campaigns/{campaign_id}/sessions/{session_id}/events/{event_id}/image
GET, PUT, DELETE /campaigns/{campaign_id}/sessions/{session_id}/initiative
POST /campaigns/{campaign_id}/sessions/{session_id}/initiative/advance
GET /campaigns/{campaign_id}/sessions/{session_id}/journal
POST /campaigns/{campaign_id}/sessions/{session_id}/rolls
GET, PUT /campaigns/{campaign_id}/sessions/{session_id}/transcript
POST /campaigns/{campaign_id}/sessions/{session_id}/utterances
PUT, DELETE, GET /campaigns/{campaign_id}/transcript/consent
GET, PATCH /campaigns/{campaign_id}/transcript/settings
GET /campaigns/{campaign_id}/voices
PUT, DELETE /campaigns/{campaign_id}/voices/{speaker_key}
GET /chunks/{chunk_id}
GET /chunks/{chunk_id}/image
POST, GET /feedback
GET /feedback/roadmap
POST /feedback/{feedback_id}/dismiss
POST /feedback/{feedback_id}/file
PUT, DELETE /feedback/{feedback_id}/vote
POST /invitations/{invitation_id}/{decision}
POST /join-links/enroll
POST /join-links/inspect
POST /join-links/redeem
GET /lobby
```

### Internal chat under `/internal/grimoire-chat`

Session, message and transcript move to private live use. Drop share, shared snapshot lookup and fork.

```text
POST /fork
POST /message
POST /session
POST /share
GET /shared/{snapshot_id}
GET /transcript
```

The shared account calls remain live under `/api/auth/platform`. Preserve GET on `/self`, `/management/users`, `/management/invitations`, `/management/invitation` and `/management/permissions`, plus POST on `/commands` and `/invitations/{id}/deliver`, with the existing permissions for each operation.

## Transfer and cutover

Rehearse a custom-format pg_dump and pg_restore into an isolated factory database. Inventory shared identity tables, foreign keys, sequences, extensions and job checkpoints before selecting tables. Copy only required rows from shared tables, retaining primary keys and membership mappings. Copy source books, chunk images, handouts and derivatives by object key to private storage. Compare object counts, sizes and checksums against a manifest. Verify row counts, references, sequence heads, representative vector searches and reader links.

At the start of the maintenance window, close Grimoire admission on the hub. Return a maintenance response at the Grimoire handlers on both Monolith profiles, including internal chat and invitation callbacks, so direct callers cannot bypass the freeze. Suspend Grimoire ingestion and embedding schedules and drain in-flight requests, uploads and jobs. Verify there are no active Grimoire writers. Keep shared account services running and reconcile any identity changes against stable subject IDs before enabling factory writes.

Take the final consistent dump and copy the object delta, including deletions. Restore and repeat the rehearsal checks. With factory writes still closed, switch only the Grimoire paths and callbacks, smoke-test gated reads and check an unrelated route on each shared Monolith profile. Enable factory writers and schedules once. Keep the old handlers closed and retain the hub database, PVCs, source objects and backups. The shared deployments continue serving other applications until their own cutovers under HOME-128.

Before the first factory write, rollback by restoring the scoped routes and callbacks, reopening the hub handlers and resuming their schedules. After writes begin, first freeze and drain factory, reconcile changed rows and objects back to the hub and verify integrity before redirecting traffic and reopening hub writes.

### Hub retirement with state retained (HOME-128)

The Grimoire route switch can finish while unrelated hub routes still have traffic. Once the last shared Monolith consumer has cut over, HOME-128 scales the hub application deployments to **zero replicas** and leaves their schedules suspended. Record the previous replica counts, release revision, database backup reference and private object manifest before scaling down. Keep the source Postgres cluster and its PVCs, the private object store and the verified dump independently of the application release. Removing Grimoire code must not cascade to those resources.

Prove the cold-source case during rehearsal: restore the dump and object copies with the source application offline, then exercise factory reader images, private campaign writes and retrieval. At hub retirement repeat those probes with zero ready hub application pods. Record the checksums and restore evidence with HOME-128. If rollback needs the hub after scale-down, restore its recorded release and replicas behind closed routes before applying the reconciliation procedure above.

Treat every dump, object manifest, private object copy and restore log as restricted migration material. Scope the transfer identity to private storage. Copyrighted text, scans and book-derived entities, adventures, summaries and embeddings stay outside the public sink throughout rehearsal, cutover and retention.

## Follow-up HOME issues

The slices are filed under [HOME-178](https://factory-plan.jomcgi.dev/factory/issue/HOME-178) in dependency order. Each issue holds its slice's numbered Done when criteria below ([record](https://factory-plan.jomcgi.dev/factory/issue/HOME-257#comment-4196)).

### Provision factory data and private runtime

Tracked in [HOME-394](https://factory-plan.jomcgi.dev/factory/issue/HOME-394).

1. Factory can restore a rehearsal dump with identity relationships, pgvector indexes and matching private object checksums.
2. API and worker service accounts reach only required stores and inference endpoints, and a backup restores successfully.

### Move gated corpus and images

Tracked in [HOME-395](https://factory-plan.jomcgi.dev/factory/issue/HOME-395).

1. Every gated page and API above resolves existing book, entity and adventure IDs including chunk permalinks.
2. Missing or expired admission denies direct content and image reads. Authorized images render with hub imgproxy disabled.

### Move campaign, operator and account tools

Tracked in [HOME-396](https://factory-plan.jomcgi.dev/factory/issue/HOME-396).

1. Test operator, GM, player and nonmember flows through OIDC, invitations, sheets, initiative, handouts, dice, journal, sessions and transcripts.
2. Route inventory leak tests pass and private retrieval respects grants. Workers resume from restored state.

### Publish static landing and replay

Tracked in [HOME-397](https://factory-plan.jomcgi.dev/factory/issue/HOME-397).

1. Reviewed original assets and an approved transcript reach public R2 through HOME-137. Audit the complete artifact manifest for restricted material.
2. Playback loads on intent and completes with the live API offline. Legacy public chat writes and shared snapshot links are retired.

### Cut over and remove hub code

Tracked in [HOME-398](https://factory-plan.jomcgi.dev/factory/issue/HOME-398).

1. Complete the frozen final transfer, verify integrity and exercise rollback before moving traffic. Confirm legacy deep links and access boundaries on production hosts.
2. Verify factory ingestion, campaign writes and static playback with zero active hub Grimoire jobs and no traffic reaching its retired handlers. Verify unrelated hub routes still serve traffic. Retain hub state and backups through rollback acceptance.
3. After acceptance remove hub-only Grimoire registrations, pages, workers, routes and imgproxy dependencies while preserving shared consumers and the retained state. Record path ownership for HOME-261.
