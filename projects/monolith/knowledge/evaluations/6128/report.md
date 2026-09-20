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
redaction, not a claim that the authorized tool exposed data improperly.

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
