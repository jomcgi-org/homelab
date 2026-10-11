# Bounded knowledge retrieval baseline

This report evaluates the existing `search_knowledge` retrieval surface for
issue [#6128](https://github.com/jomcgi-org/homelab/issues/6128). It does not
change retrieval, seed the knowledge graph, create infrastructure, or implement
any proposed fix.

## Boundaries and provenance

The original declaration is commit
`98800f7d68c2e9eea183b2103a2b869e9ce5f39f`. A public-source audit found
provenance and rubric defects before any scored call. The corrected declaration
is commit `dc68d156b53776dbecab315f11ef61b9eaf1f6e8`. `dataset.json` records every
deviation from the original declaration.

The ten questions are evaluator-written syntheses. They are grounded in linked
public issues, comments, pull requests, and immutable repository files, but are
not represented as verbatim historical agent queries. Eight are positive cases
and two require dated operational evidence before a candidate could safely
support an answer.

No durable observations from the ceased prior guest were available. This is a
new run collected from 2026-09-20T06:59:45.835Z through
2026-09-20T07:00:08.864Z, after the corrected declaration. Each query was sent
once with `limit=20` and `type=null`. The authorized MCP surface has no
`scope_filter`, so the conceptual repository scope was not transmitted.

## Results

| Query | Category | Useful top candidate | Evidence coverage | Current or conflict handling | Safe no-answer handling | Client latency |
| --- | --- | --- | --- | --- | --- | ---: |
| q01 | known issue/workaround | pass | partial | n/a | n/a | 1,935 ms |
| q02 | known issue/workaround | pass | partial | pass | n/a | 2,423 ms |
| q03 | current vs stale | fail | partial | fail | n/a | 1,739 ms |
| q04 | current vs stale | fail | none | fail | n/a | 2,523 ms |
| q05 | conflicting facts | fail | partial | fail | n/a | 1,729 ms |
| q06 | conflicting facts | fail | none | fail | n/a | 2,195 ms |
| q07 | component/path lookup | fail | partial | n/a | n/a | 3,014 ms |
| q08 | component/path lookup | fail | partial | pass | n/a | 3,940 ms |
| q09 | no answer | pass | n/a | n/a | pass | 2,191 ms |
| q10 | no answer | pass | n/a | n/a | pass | 1,255 ms |

The aggregate metrics are:

- top-result usefulness: 4/10 (40%);
- positive-query evidence coverage: 0 full, 6 partial, 2 none, out of 8;
- stale or conflicting handling: 2/6 (33.3%);
- safe no-answer candidate handling: 2/2 (100%);
- returned candidates: 200, including 9 judged to support declared evidence;
- scope mismatches: 42/200 (21%);
- unrelated candidates: 66/200 (33%);
- inappropriate candidates: 88/200 (44%), the union of scope mismatches and
  candidates judged unrelated to the declared question;
- client-observed latency: 10 calls, minimum 1,255 ms, median 2,193 ms,
  nearest-rank p95 3,940 ms, and maximum 3,940 ms;
- tool call errors: 0.

`observations.json` preserves query order, exact arguments, timestamps, client
latency, rank, returned metadata, and candidate-level judgments. Candidate
content outside the requested public repository scope is withheld while rank,
score, scope class, and denominator remain present. This is a public-artifact
redaction, not a claim that the authorized tool exposed data improperly. Three
snippets name retired ADR files; those displayed paths use a placeholder and
the exact path is stored as reconstructable segments so this frozen evidence
does not become a live documentation reference.

## Observed retrieval failures

The failures below are ranked by likely effect on agent work. Each proposed fix
is the smallest response supported by this run. No fix is implemented here.

1. Repository scope consumed 42 candidate slots. q02 and q07 each returned 15
   unscoped candidates. Expose the existing store `scope_filter` on the MCP
   method and pass the repository scope through the authorized caller.
2. Exact issue and PR dispositions were missed or stale. q05 returned the old
   #5250 needs-human state at rank 15 without its later close decision. q06
   returned none of #6043, #6048, or merged successor #6178. q03 returned
   current ARM64 support at rank 5 but not #3923's closed state. Add an exact
   identifier tie-breaker and use existing validity or supersession metadata to
   prefer the latest disposition.
3. Exact repository paths were weak. q04 returned no requested Factory path,
   q07 returned the retrieval concept at rank 4 without either module path, and
   q08 returned `bazel/ARCHITECTURE.md` at rank 14 but not
   `bazel/ocaml/README.md`. Add an exact path-token tie-breaker to the existing
   ranking path.
4. Workaround evidence was incomplete. q01 retrieved off-LAN MCP recovery but
   not the signature and capture-before-clear guidance. q02 retrieved inertness
   and the delta liveness rule but not the SSH and systemd removal path. Keep
   adjacent remediation steps with the best evidence chunk during existing
   extraction.

## Missing source data

No query was reclassified because a returned candidate supplied previously
missing ground truth. In particular:

- q09 returned no candidate tying an exact git revision to a dated observation
  of the live knowledge service;
- q10 returned the decision that teardown would occur with a move, but no dated
  observation of the systemd unit or installed files.

These are safe outcomes for this run, not proof that no dated source exists.
The absence of deployment metadata in a tool response and the closure of a
teardown issue do not establish either operational fact.

## Unavailable measurements

The retrieval-only procedure cannot measure:

- the deployed knowledge service configuration or git revision;
- server-side processing latency;
- generated-answer appropriateness or model abstention;
- authorization enforcement or access-control leakage;
- repository-filtered quality, because the authorized tool does not expose its
  store's existing `scope_filter` parameter.

Client timing includes transport and tool orchestration. It must not be read as
server processing time.

## Limitations and reproduction

This is a deliberately small ten-query baseline with one observation per
question. The service and corpus can change after the recorded timestamps.
Candidate judgments are manual and inspectable in `observations.json`. Full
notes were not fetched, so scoring uses only the evidence the search call
returned. Unscoped content is redacted from the public artifact.

To repeat the baseline, follow `procedure.md` in dataset order. A repeat is a
new observation run, not a replay or replacement of these measurements. Record
the then-current declaration, timestamps, exact tool arguments, returned rank
and metadata, client-only latency, redactions, and any newly available deployed
revision separately.

## Scoped rerun for #6713, 2026-10-10

PR [#6737](https://github.com/jomcgi-org/homelab/pull/6737) added an optional
`scope` argument to the `search_knowledge` MCP tool. Issue
[#6713](https://github.com/jomcgi-org/homelab/issues/6713) asks for the ten
baseline queries to be rerun with that scope and the off-scope slot count
reported against the 42/200 baseline. This section records that rerun. The
full record is `observations-scoped-6713.json`.

Each query was sent once, in dataset order, with the exact query text,
`limit=20`, `type=null` and `scope="repo:jomcgi-org/homelab"`. No query was
rephrased or rerun and `get_note` was not called. The calls ran from
2026-10-10T14:38:46.151Z through 2026-10-10T14:43:44.718Z.

Returned candidates per query:

| Query | Returned | Off-scope |
| --- | ---: | ---: |
| q01 | 1 | 0 |
| q02 | 20 | 0 |
| q03 | 20 | 0 |
| q04 | 19 | 0 |
| q05 | 18 | 0 |
| q06 | 20 | 0 |
| q07 | 20 | 0 |
| q08 | 15 | 0 |
| q09 | 20 | 0 |
| q10 | 17 | 0 |

Off-scope slots: 0/170 (0%), against the baseline 42/200 (21%). Every
returned candidate carried `scope: repo:jomcgi-org/homelab`. No candidate was
withheld, because no off-scope or personal candidate appeared.

Five queries (q01, q04, q05, q08, q10) returned fewer than 20 candidates. The
scope filter does not always fill the result budget, so the denominator is the
170 candidates returned, not 200. The two ratios therefore have different
denominators and are not a like-for-like comparison of result quality.

Usefulness, evidence coverage, stale or conflicting handling, and safe
no-answer handling were not re-judged. This rerun measures scope only. The
returned candidate sets differ from the baseline because the corpus changed
between 2026-09-20 and 2026-10-10, and that difference is out of scope here.

Latency is approximate client wall time around an MCP call from a cloud
session. Each timestamp came from `date -u` in a separate shell call before
and after the tool call, so the interval includes model turn overhead and tool
orchestration. It is not server processing latency. Over the nine calls with a
clean measurement the minimum was 5,707 ms, the median 7,748 ms and the maximum
15,320 ms. The q02 after timestamp was delayed by a refused client-side shell
command, so its 49,794 ms overstates the call and is excluded from that
summary. The JSON keeps both the full and the clean summary.

## Scoped rerun for #6956, 2026-10-11

Issue [#6956](https://github.com/jomcgi-org/homelab/issues/6956) asks why the
scoped rerun for #6713 returned short lists (q01 returned 1 candidate) and
whether scope is applied after a global top-k. This section records a second
scoped rerun and the answer. The full record is
`observations-scoped-6956.json`.

Each query was sent once, in dataset order, with the exact query text,
`limit=20`, `type=null` and `scope="repo:jomcgi-org/homelab"`; every other flag
was left at its default. No query was rephrased or rerun and `get_note` was not
called. The calls ran from 2026-10-11T00:25:59Z through 2026-10-11T00:28:54Z.
Any query that returned fewer than 20 got one more call with
`include_history=true` and otherwise identical arguments.

| Query | Default count | `include_history` count | Baseline rank-1 note in default list |
| --- | ---: | ---: | --- |
| q01 | 5 | 20 | No (rank 1 of the `include_history` list) |
| q02 | 20 | not run | No |
| q03 | 20 | not run | No |
| q04 | 20 | not run | No |
| q05 | 20 | not run | No |
| q06 | 20 | not run | No |
| q07 | 20 | not run | Not checkable (baseline rank 1 was withheld) |
| q08 | 20 | not run | No |
| q09 | 20 | not run | No |
| q10 | 20 | not run | No |

Every one of the 185 returned candidates carried
`scope: repo:jomcgi-org/homelab`, so there were 0 off-scope slots and nothing
to withhold. The #6713 run returned 170
candidates, with five queries under 20 (q01 1, q04 19, q05 18, q08 15, q10 17).
Nine of ten queries now fill the budget.

### Scope is a pre-LIMIT predicate

The hypothesis in the issue, that scope filters a global top-k, is false on
main. In `projects/monolith/knowledge/store.py`, `_rank_search_chunks` adds
`_scope_predicate` to the grouped notes query as a `WHERE` clause (lines
357-359), together with the legacy, deployment-observation, invalidated and
freshness filters, and only then calls `notes_stmt.limit(limit)` (line 381). The
`MIN_SEARCH_SCORE` cut (0.4) is a `HAVING` clause on the same statement. The new
PostgreSQL regression test
`test_scoped_search_returns_top_k_within_scope_not_scoped_subset_of_global_top_k`
in `exact_search_postgres_test.py` seeds higher-scoring notes in another scope
and shows that a scoped `limit=3` call returns the three best in-scope notes,
where a post-filter would return none. No ranking or authorization code
changed.

### q01 comparison against a client-side filter

The verification step from the issue was run for q01: one unscoped call with
`limit=50` and default flags, filtered client-side to the repository scope,
compared with the scoped `limit=20` list. The unscoped call returned only 5
candidates, all in `repo:jomcgi-org/homelab`, so the scope filter discarded
nothing and q01 is short with or without it. The two lists hold the same five
note ids. The order differs by one adjacent swap at ranks 2 and 3
(`the-report-recommends-staged-activation-or-a-fail-closed-bound-pod-check-before-asserting-node-wide-ownership`
and `the-dice-tray-rehearsal-had-to-target-the-advantage-radio-after-its-selector-stopped-matching`).
Their scores were 0.43158 and 0.43137 in the scoped call and 0.43114 and 0.43151
in the unscoped call, a gap of 0.0002 to 0.0004. Scores for the same notes also
moved by up to 0.0003 between the two calls. So the lists are equal as sets and differ as ordered sequences by a near-tie. No non-repository
candidate appeared, so none was withheld.

### Why q01 is short

With `include_history=true`, q01 returned 20 candidates. Rank 1 is
`mcp-sync-clears-wedged-argocd-operations` (score 0.634, freshness
`unknown`), the note that ranked first in the 2026-09-20 baseline. All 20
history candidates have freshness `unknown` and scores from 0.507 to 0.634,
and none of the five default candidates (scores 0.402 to 0.444) is among them.

The cause is the default freshness filter. The score floor does not explain it. Commit
ce741ec95 (`feat(knowledge): enforce temporal review deadlines`, 2026-10-03)
made `current_predicate` in `knowledge/freshness.py` a default filter. It hides
notes whose `review_after` is NULL, which the API reports as freshness
`unknown`. That landed after the 2026-09-20 baseline, so older notes with no
review deadline dropped out of default results. Backfilling `review_after` for
them is owned by open issue
[#6812](https://github.com/jomcgi-org/homelab/issues/6812). The 0.4
`MIN_SEARCH_SCORE` floor is a second, smaller limit: after the freshness filter,
q01 has only five notes at or above 0.4 anywhere in the corpus, which is why
the unscoped `limit=50` call also stops at five.

### Result against the acceptance

Eligible means in scope, current freshness, not legacy, not invalidated, not a
deployment observation, and scoring at least 0.4. Nine queries (q02 to q10)
returned 20 candidates. q01 returned 5, and the unscoped `limit=50` call shows
that it has only 5 eligible notes, so it falls outside the set of queries with at least 20
eligible in-scope notes. Every query with at least 20 eligible in-scope notes
returned 20, and no query contradicts that. Usefulness and evidence coverage
were not re-judged.

Latency is approximate client wall time around each MCP call from a factory
sandbox, including model turn overhead; it is not server processing latency.
Over the nine calls with a clean measurement (all except q02, whose finish stamp
was taken after the list was transcribed) the minimum was 3,379 ms, the median
4,300 ms and the maximum 8,716 ms. The q01 stamps have one-second resolution.
