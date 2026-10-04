# Knowledge Pipeline

LLM-powered knowledge graph with on-cluster inference.

## Overview

Raw markdown is ingested, decomposed into structured facts by a remote claude.ai gardener routine over MCP (ADR 006 Phase 4c), embedded with voyage-4-nano, and stored in pgvector for semantic search. Fronted by a SvelteKit app with a `Cmd+K` search overlay.

| Module              | Description                                                                                                              |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| **ingest_queue**    | Ingests raw markdown, routes to gardener or direct storage                                                               |
| **gardener**        | Shared decomposition constants/helpers; the decomposition runs as a remote claude.ai routine over MCP (ADR 006 Phase 4c) |
| **gaps**            | Unresolved wikilink lifecycle: discover → classify → review → answer (classifier injected as a callable, fileless)       |
| **store**           | pgvector-backed storage with semantic search                                                                             |
| **service**         | FastAPI service layer                                                                                                    |
| **router**          | HTTP API routes                                                                                                          |
| **mcp**             | MCP tool exposure for AI agent access to the knowledge graph                                                             |
| **links/wikilinks** | Obsidian wikilink parsing and backlink resolution                                                                        |
| **tasks_router**    | Task management API                                                                                                      |

## Search authorization

The MCP `search_knowledge` tool and `GET /api/knowledge/search` share one
caller-derived policy. Exact `org:`, `repo:`, or `environment:` grants in the
signed principal's `scope` claim are accepted directly. The verified
`homelab-admin` and `kg-agents` groups map to the three exact homelab grants
declared by repository policy, matching their authentik blueprint memberships.
The generic `operators` group, generic OAuth scopes, and server defaults do not
authorize a search. Anonymous callers and authenticated callers without a
mapped grant fail closed.

MCP callers can supply `scope`, for example `repo:jomcgi-org/homelab`, to narrow
search to one exactly matching authorized scope; unauthorized values return the
same empty result as a search with no matches. Narrowed searches exclude legacy
NULL-scoped notes even with `include_personal=true`, and personal opt-in remains
audited before the scope check.

The `kg search` CLI currently supplies only its Cloudflare Access cookie, not
the Authentik bearer required by this policy, so human CLI search is denied.
Issue #6306 tracks acquiring and sending a separate Authentik bearer while
retaining the Cloudflare cookie for edge authentication.

Personal notes are excluded by default. `include_personal=true` is accepted only
when the same principal carries `personal:<subject>` or one of the two mapped
knowledge groups. It includes only `personal:<subject>` and legacy NULL-scoped
notes, and commits one attribution-only audit row before embedding or retrieval.
The audit row contains no raw query or returned content. An audit failure denies
the search. An empty result or a later embedding failure retains the one
committed attempt row. Rows are retained for 90 days. An insert trigger owned by
the migration role prunes older rows in the same transaction, so the agents
tier retains INSERT-only access and an audit commit cannot succeed without its
retention work succeeding too.

Internal recall and extraction callers remain separate: they pass explicit
store filters and never inherit public-entrypoint authorization. Typed graph
edges resolve only when their target is within the same search allow-list.
`POST /api/chat/explore` remains an unscoped retrieval surface and can return
personal and legacy NULL-scoped notes; issue #6307 tracks authorizing it.

`get_note` and `GET /api/knowledge/notes/{note_id}` remain separate direct-ID
authorization surfaces. This change does not make those paths safe merely
because search is filtered, and callers must not treat an ID learned elsewhere
as authorization.

### Acceptance-to-test matrix

| Acceptance | Coverage |
| --- | --- |
| MCP and HTTP default searches use exact org, repo, and environment grants | `mcp_test.py::TestSearchKnowledge`, `router_test.py::TestSearchEndpoint` |
| Personal, cross-subject, cross-repository, and NULL scopes are excluded by default | `store_scoped_test.py::test_search_scope_allow_list_is_applied_before_ranking` |
| Explicit permitted opt-in includes caller personal and NULL scopes with one durable audit | MCP and HTTP opt-in tests plus `store_scoped_test.py` |
| No opt-in creates no audit | MCP and HTTP default-search tests |
| Anonymous, unmapped, and empty allow-lists fail closed | MCP and HTTP authorization tests plus the store empty-list test |
| Authorization is applied before top-N ranking | `store_scoped_test.py::test_search_scope_allow_list_is_applied_before_ranking` |
| Audit failure denies retrieval, while empty results and embedding failures retain one audit | MCP and HTTP failure-path tests |
| Cross-scope edge targets do not resolve | `store_scoped_test.py::test_edge_resolution_uses_search_allow_list` |
| Audit retention and INSERT-only application privileges execute in Postgres | `personal_retrieval_audit_grants_test.py` |

## Temporal review policy (v1)

Every ordinary write computes a server-side review policy from the persisted
title and claim body. A claim is volatile (`volatile-24h/v1`) when a
concrete instance is asserted in a current state within one sentence: a PR or
issue (`PR #6821`, `owner/repo#12`, a GitHub URL), a run or job id, or a commit
SHA, together with a state term such as open, merged, passing, head or
outstanding. Outstanding operational work, such as a required live pilot,
waiting for a rollout or an unverified deployment, is also volatile without
a concrete instance. The classifier and verifier share that vocabulary.
A durable rule that only mentions PRs, checks, gates or rollouts
("Required CI must succeed on the exact review head") names no instance and is
standard. `## Evidence`, `## Provenance`, `## Sources` and `## References`
sections are excluded: a durable claim that cites a PR as evidence stays
standard. Everything else uses `standard-90d/v1`. Volatile is sticky: nothing
downgrades it. These are elapsed UTC
intervals of 24 and 2160 hours. Caller-supplied deadlines can only shorten them.
Confidence and verification state do not establish freshness.

`observed_at` remains the original evidence observation. Unknown, malformed or
future observations are freshness-unknown and excluded from current context.
At `now >= review_after`, a fact is due. Validity windows, supersession,
invalidation and disputes retain their separate meanings. Expiry resolves no
acceptance gate and deletes no evidence.

Upserts, indexing, extraction retellings and frontmatter round-trips preserve
the original observation and successful review time. They can only keep or
shorten the existing deadline. Extraction duplicate lookup explicitly includes
due facts so a retelling cannot create a fresh copy, and a retelling advances
the note's `revision`. Prompt-building lookups exclude due facts. Only
evidence-backed review (below) renews a deadline.

### Evidence-backed review

`Note.revision` is a monotonic counter advanced by every ORM update that
changes the claim or its support (content, confidence, state, validity,
a duplicate retelling through `bump_revision`) and by supersession. The review
lease and layout-only changes do not move it. Every store upsert, including
reindex, locks and refreshes the existing row before advancing it. ORM
retellings increment revision with database arithmetic. Open-dispute writes
update the note revision in the same transaction, serializing against the
review's note row lock. `content_hash` alone
cannot see a retelling, so a review captures the revision at admission.

`review_verifier.GitHubVerifier` verifies only what a GitHub response
establishes: issue or PR open and closed, PR merged and draft, a PR head SHA,
and check runs tied to one exact SHA (a PR's own head is resolved and recorded).
Checks at an explicit SHA naming a PR also require a valid PR and GitHub's
commit-to-PR association. Completed failed runs outrank incomplete runs.
Every sentence must fully match explicit ASCII templates: `REF is STATE`,
`REF has been merged`, `REF head is SHA`, `REF checks are TERM` (optionally
`at SHA`), `REF checks TERM at SHA`, or `Checks are TERM at SHA`,
`Checks TERM at SHA` and `Checks at SHA are TERM`. STATE is open, closed,
merged, draft or a draft; TERM maps passing/failed/pending synonyms to check
outcomes. REF is one PR/issue reference, bare number with `#`, repository
reference or GitHub URL. SHA is 7-40 lowercase hex characters containing a
letter. Complete clauses may join with `and` or `, and`, each naming its own
subject. Every reference and SHA produces a predicate. Any unmatched wording
makes the whole note `unsupported` with a recorded reason, and it stays due.
**Why.** Denylist bypasses across five reviews required explicit supported
claim templates and fail-closed matching.

Another repository, more than five references or more than 100 check runs is
also unsupported. A response that contradicts the claim is
`failed`; an unreachable source, rate limit or malformed response is
`unavailable`. Evidence time is the oldest response used, never later.

`freshness.commit_successful_review` is the only renewal. It runs inside the
caller's transaction and never commits or rolls back: the renewal and its
`knowledge.review_outcomes` row become visible together when the caller commits.
It locks the row and refuses (recording a `failed` outcome with the reason) when
the captured revision or content hash moved, the note is disputed, invalidated,
superseded or expired, the evidence is not newer than the last review, older
than the observation, or in the future. A volatile policy is never downgraded.

`knowledge.review_outcomes` keeps one row per attempt: `success`, `failed`,
`unavailable` or `unsupported`, with reason, evidence, the revision reviewed
and `next_attempt_at`. Unsupported waits for a new revision, failed for a day
or a new revision, unavailable backs off from 5 minutes to a 6 hour cap. A
success is never recorded for an unavailable or unsupported source.

`knowledge-review-admission` (`review_admission.py`, an Argo CronWorkflow with
`Forbid` concurrency) admits due volatile notes oldest first. Blocking
outcomes are excluded in SQL so unsupported notes cannot starve verifiable
ones. A run is bounded by a batch (default 20), a request budget (default 60,
responses shared across notes) and a wall-clock deadline (default 240 s), and
runs one worker; each note is verified outside any transaction and committed in
its own. The CronWorkflow lands suspended (`suspend: true`): enable it with a
values-only change after the scoped pilot. Dry run (no `--apply`) only counts
candidates. The note detail view returns `last_review_outcome`, and the private
notes panel shows freshness, the deadline and why a fact is still due,
independent of confidence.

Default search and recall apply expiry in SQL before ranking and again at
hydration. MCP and HTTP `include_history=true` expose due and unknown facts
without widening scope, personal, visibility, legacy or dispute access. Search
responses use `private, no-store`. Query-embedding caches contain no facts.
Persisted recall blocks carry their earliest deadline and are discarded by the
transport before retransmission when due. The block is located by its generated
shape (fixed preamble, its `RECALL_EXPIRES` line, then nonce-fenced notes through
to the end), so a snippet or task text that quotes the header neither keeps an
expired block alive nor truncates the task. Already sent agent transcripts are
historical snapshots, explicitly dated and labelled with their expiry.
Volatile facts require a new authoritative observation before action even
inside their 24-hour interval.

**Public tier (decision).** `public_api.knowledge_notes` and
`public_api.knowledge_chunks` do not yet apply review freshness, so a due
published fact can still ground public chat and `public_router` search. This
change covers MCP, HTTP, recall, explorer and planner context only; the public
views need a migration and a pg-backed test pass, tracked in #6823.

The existing jobs image provides `knowledge-review-backfill --apply --pending-only`
as an Argo CronWorkflow that is **suspended by default**, including with the
production values. A merge or deployment does not enable the backfill. After a
separately approved dry run and review of its counts, an operator can explicitly
set the `knowledge-review-backfill` entry in `jobs.cronWorkflows` to
`suspend: false` through GitOps. Keep `knowledge-review-admission` suspended;
backfill enablement does not authorize renewal admission. Once enabled, the
backfill runs every five minutes with `Forbid` concurrency, a five-minute
deadline and bounded resources. It handles at most 20 batches of
500 notes per run, ordered by stable note id, using only `observed_at` or a
successful review. Rows waiting for backfill have null deadlines and are
freshness-unknown and due. There is no migration-day lease. Dry run is the
default for `knowledge-review-backfill`; its JSON reports policy/freshness
counts and `next_after` for bounded continuation. Replays preserve identity,
provenance, disputes and any shorter deadline. Application commits atomically
and database errors propagate with rollback.

After explicit enablement, the five-minute schedule is deliberately perpetual.
Pods still running code from before the migration can write notes without a
policy during a rollout, and
those rows would otherwise stay freshness-unknown. With nothing pending the
query returns no rows, so it locks nothing and applies nothing. Suspend the
CronWorkflow through the job's `suspend` flag once a dry run reports no pending
rows and no older writers remain.

**Why.** Evidence describes what was observed at a particular time. A bounded
review interval limits its use as current context while keeping its historical
value. Read-time enforcement makes that boundary independent of scheduler
availability. Only evidence-backed review may establish a new freshness basis.
