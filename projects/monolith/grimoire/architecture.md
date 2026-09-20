# Grimoire architecture

Grimoire is a domain of the Python Monolith. The former standalone Go API,
React frontend, WebSocket gateway, Redis service, Helm chart, and GCP bootstrap
have been retired. Their implementation remains available in git history.

## Runtime shape

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
- `campaign`, `player_character`, `game_session`, and `knowledge_grant` hold
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

An administrator uses **Account invitations** in the lobby to open Authentik's
invitation management page. Choose **Grimoire enrollment**, enable **Single
use**, set an expiry, and put the recipient's email in **Fixed data**, for
example `{"email": "friend@example.com"}`. Deliver the generated link to that
person. Enrollment requires the invitation and fixes the email to its supplied
value; it asks for username, display name and password. New users are external
users, with no operator or family group membership. After enrollment they land
in Grimoire. Existing human Authentik users can sign in directly.

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

The first delivery includes the lobby and access to the existing sheet editor.
Player-created characters and a session screen remain separate follow-up work.

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
5. Ordinary users do not see the account-admin link and cannot access
   Authentik administration. Operators see a link to
   `/if/admin/#/flow/stages/invitations`.
6. The moving app and preview lane retain their original access policies.
   Shared `/_app/` assets accept either app's signed cookie, while page and API
   permissions remain separate. Requests to `/api/grimoire` on the friends
   hostname have no backend route.
