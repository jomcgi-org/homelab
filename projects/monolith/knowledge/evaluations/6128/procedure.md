# Knowledge retrieval baseline procedure

This directory is the bounded baseline requested by issue
[#6128](https://github.com/jomcgi-org/homelab/issues/6128). It evaluates the
existing retrieval surface. It does not add data to the knowledge graph, change
retrieval, or treat search results as generated answers.

## Declaration and ordering

`dataset.json` freezes evaluator-written questions grounded in real agent work,
source-backed expected evidence, safe no-answer conditions, and the rubric
before any scored call. These are not represented as verbatim historical agent
queries. The linked public records establish the work and judgment evidence.

Commit `98800f7d68c2e9eea183b2103a2b869e9ce5f39f` is the original declaration
boundary. A source audit found rubric and provenance defects before any scored
calls were collected. Schema version 2 records those deviations and supersedes
that declaration. The commit introducing schema version 2 is the scored-run
declaration boundary. Observations must be collected only after that commit and
recorded in a later commit. This ordering prevents selecting questions or
expectations after seeing results while preserving the original history.

The source links are public GitHub issue records and immutable repository links.
No private transcript is required to judge an expectation. An empty
`expected_evidence` list is intentional only for a no-answer case and is paired
with dated-source requirements and a public `known_source_limit`.

## Retrieval procedure

For each query in dataset order:

1. Call the supported agent MCP `search_knowledge` tool exactly once with the
   exact query text, `limit=20`, and `type=null`.
2. Measure client-observed elapsed wall time around the call. Record it only as
   client latency. Do not infer server processing latency.
3. Preserve rank and returned metadata needed to inspect the judgment. Never
   call `get_note`, rephrase, rerun, or seed expected material before scoring.
4. Request repository scope conceptually, but record the effective scope as
   unfiltered because the MCP surface does not expose `scope_filter`. A result
   outside `repo:jomcgi-org/homelab` is a scope mismatch for this baseline. It
   is not, without separate evidence, an access-control finding.
5. Withhold private or potentially personal returned content from the public
   artifact. Preserve its rank, scope class when safe, and judgment as
   `withheld_private` so denominators remain reproducible.
6. Judge each returned candidate against only the predeclared evidence. The
   retrieval tool does not generate an answer or expose an abstention signal.
   For no-answer cases, check whether a candidate purports to establish the
   operational fact and whether it meets every dated-source requirement.
7. Record observed retrieval failure separately from missing source data and
   from an unavailable measurement. A newly discovered candidate meeting all
   no-answer requirements is missing declaration source data, not a retrieval
   failure.

The deployed service revision is recorded only if the tool response exposes it.
The checkout SHA, dataset declaration commit, and observation commit describe
the evaluation artifact, not the live deployment.

## Metrics

Report the actual query count and these denominators:

- top-result usefulness: useful top results divided by all queries;
- evidence coverage: full, partial, and none counts divided by positive queries;
- stale or conflicting handling: passes divided by applicable queries;
- safe no-answer candidate handling: passes divided by no-answer queries;
- inappropriate candidates: count divided by all returned candidates, with
  scope mismatches reported separately;
- observed client latency: count, minimum, median, p95, and maximum over calls
  with a measured duration.

The report must also say whether the top candidate was useful for each query,
list the evidence coverage judgment, identify stale or conflicting behavior,
and rank only concrete measured failures by impact on agent work. Generated
answer quality, actual model abstention, access-control enforcement, and server
processing latency are unavailable from this retrieval-only procedure.
Proposed fixes must be the smallest response supported by these observations.
