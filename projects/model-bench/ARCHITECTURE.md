# Model bench

Model bench runs frozen task fixtures through candidate tool loops and hidden
verifiers. Result cells cache model costs, correctness gates and optional graded
scores. CLI reports aggregate those cells, excluding harness errors.

## Performance grading

The `speedup` verifier's opt-in `paired-v1` contract copies allowlisted candidate
files into an isolated temporary grading directory. A stdlib-only trusted helper
checks baseline oracles, loads candidate code after AST screening, and measures
fresh identical inputs in alternating pairs. The parent verifier validates raw
samples and computes scores. Helper source and task verifier arguments are part
of the cache identity. Legacy tasks retain their existing grading contract.
Pure-function task harnesses opt into input-mutation checks outside timing.
The climatology task pins a naive fixture with exact integer sums and the
endpoint's first-valid-row stable tie order. Its references remain model-hidden.
The campsites rollup task pins exact integer counts and scores, the inclusive
today-minus-one through today-plus-13 window, and independent last-row-wins
availability/weather maps. Empty campgrounds produce an explicit 503 record.
Both packs seed visible tests while keeping the protected oracles and calibrated
references outside candidate fixtures.

**Why.** Correctness-gated buckets distinguish an algorithmic improvement from a
fast wrong answer. A discarded warm-up and seven alternating pairs reduce order
and scheduling noise; the median paired ratio limits outliers. Wide buckets give
partial credit without implying precision beyond the frozen dataset and runner.
Issue #6696 records this choice. Seeded naive baselines disclose their provenance
and fixture version; they make no claim about current production algorithms.

## Direction

| Direction | Decided in | Tracks | State |
| --- | --- | --- | --- |
| Correctness-gated paired-median speedup buckets on frozen seeded fixtures | Performance grading | #6696 | Infrastructure, climatology and campsites rollup fixtures implemented; repository-only delivery |
