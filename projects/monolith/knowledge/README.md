# Knowledge Pipeline

LLM-powered knowledge graph with on-cluster inference.

## Overview

Raw markdown is ingested, decomposed into structured facts by a remote claude.ai gardener routine over MCP (ADR 006 Phase 4c), embedded with voyage-4-nano, and stored in pgvector for semantic search. Fronted by a SvelteKit app with a `Cmd+K` search overlay.

| Module              | Description                                                                                                              |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| **raw_write**       | Persists raw markdown (content-addressed) to Postgres and the S3 raw store                                               |
| **gardener**        | Shared decomposition constants/helpers; the decomposition runs as a remote claude.ai routine over MCP (ADR 006 Phase 4c) |
| **gaps**            | Unresolved wikilink lifecycle: discover → classify → review → answer (classifier injected as a callable, fileless)       |
| **store**           | pgvector-backed storage with semantic search                                                                             |
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

**Public tier (staged).** `public_api.knowledge_notes` exposes the review
columns. Public retrieval joins chunks to notes and applies the shared current
predicate before its limit. Public search, note detail, graph nodes and edges,
entity chapters and note counts use the same caller-clock rule. Daily fact
counts remain aggregate history without fact text. With backfill suspended,
unclassified public notes stay hidden until an operator-approved backfill gives
them a lease. Current-only HTTP responses cap browser and shared lifetimes at
the earliest served review deadline, remove stale-serving windows and use
fact-set validators. The same-origin proxies bound TTLs to the upstream policy,
consuming upstream age, and each hop dates its own response instead of
forwarding upstream Date/Age as a new lease basis. Cloudflare and browsers
start the TTL at receipt, so the origin lease is measured from serve time.
Public chat hashes current notes and their deadlines, caps its watermark
memo at the first deadline and rechecks touched notes on every cache hit.
Migration rollout and operational acceptance remain on #6823. Publication
policy and stored history are unchanged; dated-history presentation is follow-up
work.

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


### Run the bounded operational pilot

The suspended `knowledge-review-backfill-dry-run`,
`knowledge-review-backfill-pilot` and `knowledge-review-admission-dry-run`
CronWorkflows provide fixed arguments for #6812. Backfill is limited to 20
pending notes in one batch and a 240-second workflow deadline. Admission dry
run counts at most 20 candidates without GitHub requests or writes. These
entries do not enable recurring mutation jobs.

After approved merge, chart publication and verified deployment, an operator
with the existing Argo submission access can run:

```bash
argo submit --from cronworkflow/knowledge-review-backfill-dry-run -n monolith-workflows
argo submit --from cronworkflow/knowledge-review-admission-dry-run -n monolith-workflows
```

Record each workflow's JSON result on #6812, including policy/freshness counts,
`next_after`, candidate and blocked counts. Inspect the bounded backfill dry
run before submitting `knowledge-review-backfill-pilot` with the same command
shape. Save before/after note IDs, provenance, disputes, observation dates and
deadlines; unknown dates must remain due. Repeat the dry run to confirm that
applied rows leave the pending set. A nonempty continuation is further work,
not permission to loop through the corpus.

The existing `knowledge-review-admission` job applies one batch with at most
20 notes and 60 GitHub requests, a 240-second cutoff before starting another
note, and a 300-second workflow deadline. Submit it only after reviewing
dry-run counts and confirming no admission workflow is active. `Forbid`
controls scheduled runs; the shared mutex also serializes manual execution.
Capture its outcome counts and authoritative evidence for actual renewed note
IDs. Confirm `last_reviewed_at` and `review_after` moved from that evidence,
original `observed_at` stayed unchanged, and unavailable/unsupported outcomes
left notes due. The operator-only MCP `submit_kg_review_pilot` now submits these
fixed jobs without a legacy scheduler row. `inspect_kg_review_pilot` returns
only control status and audited JSON results; it does not return manifests,
credential references or pod logs.

Use a canonical UUID `request_id` for each proposed submission. Reuse that ID
after a timeout or lost response. Inspect the dry-run result before requesting
application, passing its ID as `dry_run_request_id`. The server requires a
matching successful, nonempty dry run from the last 15 minutes, and each dry
run can authorize one application. Neither tool accepts argument, namespace or
manifest overrides. Both require standing human membership in `operators`.

A durable database receipt records the operator, job, request ID, workflow name
and observed results. A unique active slot serializes API submissions across
replicas; all five review CronWorkflows also share an Argo controller mutex for
manual and scheduled execution. Inspection records terminal results before
releasing the slot. A failed Kubernetes call or missing Workflow leaves the
receipt fenced. An operator must investigate that uncertainty; the tools offer
no force release or resubmit path. Retained receipts prevent reuse of a request
ID after Argo garbage collection. Submit refuses drifted arguments or bounds,
active review workflows, and unsuspended review controls.

The existing monolith Role supplies CronWorkflow list access. The platform's
`monolith-workflow-submit` Role supplies Workflow create/get/list in
`monolith-workflows`; this extension adds no RBAC grants. Tool discovery and
operator identity must be verified on the deployed MCP before claiming that
a client can run the pilot. JSON reports list the bounded candidate note IDs
and committed outcomes; retrieve renewed notes to inspect authoritative review
evidence and deadlines. These controls do not enable ongoing scheduling.

Keep recurring admission suspended until pilot evidence is reviewed. Enable
its existing 15-minute schedule through a separate approved GitOps change,
then verify live suspension state, schedules, limits and two scheduled workflow
receipts. Roll back scheduling with `suspend: true` through GitOps and allow any
bounded in-flight run to finish. Do not restore old deadlines or erase review
history. Keep the manual pilot entries suspended throughout.

This pilot selects already-due volatile notes only. Standard 90-day notes and
pre-expiry review need separate scoped admission work with explicit source
coverage and the same aggregate budgets. Labels such as `agent-ready`, checkout
contents and free-text acceptance gates are unsupported by the GitHub verifier.
Changed claims currently record failure; automatic supersession is still an
operational acceptance gap on #6812. Never rewrite a note into a supported
claim or extend its deadline merely to obtain a successful pilot result.
