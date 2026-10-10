# Model bench

The CLI runs task fixtures through file-editing agents and deterministic
verifiers. Cached result cells feed Markdown and JSON reports. Single-shot
candidates are text outputs and do not receive file-edit norms.

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

## Direction

Nothing is decided and unbuilt here at present; outstanding work is
tracked in GitHub Issues.
