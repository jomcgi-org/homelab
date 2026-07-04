# Agentic Friction Remediation Plan

Companion to `2026-07-03-agentic-friction-spec.md`. One PR, multiple commits,
one comprehensive end-of-PR review. Tasks ranked by leverage; each cites the
spec pattern it addresses.

## Task 1: Idempotent chart publish + missed-bump detector (F1, core)

`bazel/helm/push.sh.tpl`, main-branch path only:

1. Before `helm push`, check whether the chart's current `version:` already
   exists in the OCI registry (`helm show chart oci://<repo>/<name> --version <v>`).
2. If it exists: **skip the push** (never mutate a published version in place;
   kills the same-version republish wedge, 8 occurrences in the window).
3. After skipping, run the existing `chart-version.sh` closure analysis: if it
   says a bump is due (releasable commits since the version was last set),
   **exit 1 with a precise message** naming the chart, current version, computed
   next version, and the exact `bump-chart.sh` command to run. This converts
   "merged code silently never deploys" (6+ occurrences) into a loud main-CI
   failure minutes after merge.
4. If the existence check itself errors (registry flake), log a warning and fall
   through to today's behavior (push) — no new hard dependency on registry reads.

PR-branch behavior (dev-tag republish, bot bump) unchanged.

## Task 2: `bump-chart.sh` helper (F1)

New `bazel/tools/git/bump-chart.sh <projects/<svc>>`:
fetch `origin/main`; read the chart version at the main tip (not the local
checkout — kills the version-race class); compute next patch (or `--minor` /
explicit version); write `chart/Chart.yaml` + `deploy/application.yaml`
`targetRevision` in sync; print what changed. Add to CLAUDE.md Essential
Commands. This makes each of the 20-per-window bump events a one-command,
race-free operation, including the "rebump after rebase collision" case that
cost PR #3157 five cycles / 68 minutes.

## Task 3: Hook exec-bit fix + liveness guard (F5)

- `chmod +x` (git mode 100755) `check-large-migration-sql.sh` and
  `check-lifespan-startup-mock-sync.sh` (dead since 2026-06-13; 256+ silent
  exit-126 failures across two sessions alone).
- New `bazel/tools/hooks/validate-hooks-executable.sh`: parse
  `.claude/settings.json`, assert every configured hook file exists and is
  executable. Wire into the Format check CI step and pre-commit so a dead hook
  can never ship silently again.

## Task 4: Semgrep Semgrepignore-v2 glob cleanup (F2/F6)

Rewrite bare `paths:` globs (`*_test.py` → `**/*_test.py`, etc.) across
`bazel/semgrep/rules/` so the pre-commit semgrep hook stops emitting ~40
deprecation-warning lines per commit that poison exit codes and force manual
noise-filtering. Verify against the semgrep fixture tests.

## Task 5: ArgoCD ops runbook + API helper (F1/F3/F6)

- `docs/runbooks/argocd-outofsync.md`: decision tree distilled from the
  2026-07-04 sagas: stale chart-render cache (force sync), duplicate env-name
  collapse, app-level `ignoreDifferences` inert (use global cm), stuck-sync
  termination patch, canada wedge modes, `resourceHealthSource: appTree`,
  "which commit is this chart version built from".
- `bazel/tools/cluster/argocd-api.sh <path>`: the port-forward + admin secret +
  session JWT + curl dance sessions hand-roll 4x/minute today.
- CLAUDE.md Cluster Investigation pointer.

## Task 6: Public-tier preflight (F3)

- `docs/runbooks/public-tier-checklist.md`: the four documented gotchas
  (public_reader grants incl. every table, same-origin `/api` proxy,
  gazelle-exclude srcs in the public binary, is_global filtering) plus
  post-deploy curl verification steps.
- New Write/Edit hook `check-public-reader-grant.sh`: block a migration that
  `CREATE TABLE`s in a public-served schema without a `public_reader` GRANT in
  the same file (schema list derived from existing grants). With test.
- CLAUDE.md Context Loading rule pointing at the checklist.

## Task 7: Merge/watch ergonomics (F4/F6)

CLAUDE.md adjustments: `gh pr merge --auto --rebase` fails with "clean status"
on already-green PRs (fall back to plain `--rebase`); prefer
`run_in_background` / Monitor over sleep-chains for CI waits; post-merge deploy
verification is now signaled by Task 1's loud main-CI failure instead of manual
`git log origin/main -- <chart>` checks. BES-flake retrigger recipe goes in the
runbook.

## Task 8: apko checksum self-service (F3/F6)

Verify `update-apko-locks.sh` regenerates `config.checksum` for
apko.yaml-only edits; if its path is heavy or misses that case, add the
documented sha256-base64 patch recipe as a fast path or a
`fix-apko-checksum.sh`. Reference from the runbook. (Time-boxed.)

## Task 9: Duplicate env-name render check (F1)

Extend `bazel/helm/ci-diff-manifests.sh` to scan the PR's rendered manifests
for duplicate `env[].name` within a container and fail loudly. Catches the
phantom-OutOfSync class (#3158) at PR time instead of after a 2-hour prod saga.

## Task 10: ADR for chart publish idempotency (F1)

`docs/decisions/platform/`: rationale for skip-if-exists + loud missed-bump
detection (Accepted, implemented by Task 1). Records the rejected alternatives
(content-digest comparison — impossible under stamped tags; post-merge
auto-bump bot and semver-range targetRevision tracking — left open, Joe's call).

## Task 11: Docs/memory truth-up (F1)

CLAUDE.md deploy-pattern row updated to the precise semantics after Task 1
(PR-branch bot bumps when the closure detects changes; main publishes only new
versions and fails loudly on missed bumps). Update the related memory files
post-merge.

## Execution

Single PR from `feat/agentic-friction-fixes`. Implementation fan-out: Sonnet
implementers on disjoint file sets (Tasks 1+2, 3, 4, 5+6 docs, 8, 9); CLAUDE.md
integration and Task 10 by the main session. One comprehensive Opus-tier review
of the full diff before push; CI on the pushed branch is the test gate; merge
on green (rebase). No chart bumps required (no service chart content touched).
