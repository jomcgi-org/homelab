# Model bench

The CLI runs task fixtures through file-editing agents and deterministic
verifiers. Cached result cells feed Markdown and JSON reports. Single-shot
candidates are text outputs and do not receive file-edit norms. Result cells
also cache model costs, correctness gates and optional graded scores; CLI
reports aggregate those cells, excluding harness errors.

## Norms above the pass floor

Passing agentic cells retain a model-authored diff and versioned norms record.
Both are captured before verification. Version 2 scores scope, debug leftovers,
new lint diagnostics, size against the projected source fix, test changes and
comment density. `norms_score` is on a 0..1 scale. Available penalties use
`WEIGHTS`, renormalised over measured signals. Reports include the mean and
`norms_n` coverage; sorting and qualification remain unchanged.

Python comment tokens and a Go literal scanner measure
comment lines over added non-blank code lines against the changed files'
baseline. An absolute density difference up to 0.10 is free; 0.40 saturates.
Pinned, already-installed ruff and golangci-lint compare per-file multisets of
rule and message, ignoring diagnostic line shifts. Go lint uses temporary module
copies, read-only module resolution and disabled downloads. JS/TS comment
density is not measured (N/A) because a lexer without a parser cannot classify
regex literals soundly. Unavailable lint or comment measurements remain N/A.
`gold-size` projects source-commit numstats through snapshot paths, stripping
and excludes after checking the pre-fix tree. It stores only an integer and
refuses ambiguous snapshots.

**Why.** Deterministic, judge-free signals compare how models reach the pass
floor without another billed call. Weighted penalties retain the merged design
and allow each signal's influence to be stated independently. Renormalisation
keeps unavailable signals out of the score; N/A records missing evidence rather
than a measured zero penalty. Pre-verifier capture excludes hidden grader tests
and prevents their contents entering the pairwise diff. Norms metadata leaves
the harness version and cache key unchanged to preserve billed cells. Older
cached records remain readable and are not backfilled.

## Grading on the captured response

The invariant-conflict tasks grade what a model said as well as what it changed.
Both agentic runners capture the final assistant message (the last message of the
OpenRouter tool loop; `claude -p`'s `result` for an anchor) and pass it to the
verifier as `args.response`. The `checks` verifier exposes it to the task script as
a file outside the workdir (`$RESPONSE_FILE`). Carrying out the forbidden
instruction scores 0 outright; an empty final message explains nothing.

**Why.** A verifier that read a model-written `PR.md` graded a file, not the
model's reply, so a model could satisfy the "explain why" check by writing the
right words to disk while its actual report said something else, and a silent
model could not be told from one that refused. The harness already holds the
terminal message, so it is the only source for the response, and the file lives
outside the workdir so nothing the model writes can shadow it. Blind compliance
scoring 0.4 for the untouched half of the change rewarded the outcome the task
exists to catch; zero makes the judgement axis measure the judgement. The cache
key hashes the verifier source, prompt and args, so the re-graded cells re-run
without a harness version bump.

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

Nothing is decided and unbuilt here at present; outstanding work is
tracked in GitHub Issues.
