# Knowledge retrieval baseline procedure

This directory is the bounded baseline requested by issue
[#6128](https://github.com/jomcgi-org/homelab/issues/6128). It evaluates the
existing retrieval surface. It does not add data to the knowledge graph, change
retrieval, or treat search results as generated answers.

## Declaration and ordering

`dataset.json` freezes the real-agent-work questions, source-backed expected
evidence, acceptable abstention, and rubric before any scored call. The git
commit that first adds `dataset.json` is the declaration boundary. Observations
must be collected only after that commit and recorded in a later commit. This
ordering prevents selecting queries or expectations after seeing results.

The source links are public GitHub issue records and immutable repository links.
No private transcript is required to judge an expectation. An empty
`expected_evidence` list is intentional only for a no-answer case and is paired
with a source-backed `unavailable_reason`.

## Retrieval procedure

For each query in dataset order:

1. Call the supported agent MCP `search_knowledge` tool exactly once with the
   exact query text, `limit=20`, and no type filter.
2. Measure client-observed elapsed wall time around the call. Record it only as
   client latency. Do not infer server processing latency.
3. Preserve rank and returned metadata needed to inspect the judgment. Never
   call `get_note`, rephrase, rerun, or seed expected material before scoring.
4. Request repository scope conceptually, but record the effective scope as
   unfiltered because the MCP surface does not expose `scope_filter`. A result
   outside `repo:jomcgi-org/homelab` is inappropriate for this baseline.
5. Withhold private or potentially personal returned content from the public
   artifact. Preserve its rank, scope class when safe, and judgment as
   `withheld_private` so denominators remain reproducible.
6. Judge each returned result against only the predeclared evidence. Record
   observed retrieval failure separately from missing source data and from an
   unavailable measurement.

The deployed service revision is recorded only if the tool response exposes it.
The checkout SHA, dataset declaration commit, and observation commit describe
the evaluation artifact, not the live deployment.

## Metrics

Report the actual query count and these denominators:

- top-result usefulness: useful top results divided by all queries;
- evidence coverage: full, partial, and none counts divided by positive queries;
- stale or conflicting handling: passes divided by applicable queries;
- acceptable abstention: passes divided by no-answer queries;
- inappropriate results: count divided by all returned results;
- observed client latency: count, minimum, median, p95, and maximum over calls
  with a measured duration.

The report must also say whether the top result was useful for each query, list
the evidence coverage judgment, identify stale or conflicting behavior, and
rank only concrete measured failures by impact on agent work. Proposed fixes
must be the smallest response supported by these observations.
