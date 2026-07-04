# ADR 011: Idempotent Chart Publish and Missed-Bump Detection on Main

**Author:** jomcgi
**Status:** Accepted
**Created:** 2026-07-04
**Builds on:** [009 - Post-Merge Chart Versioning and Kargo Promotion](009-post-merge-chart-versioning-kargo-promotion.md) (Draft; the fuller automation this ADR deliberately does not commit to)

---

## Problem

Every merge to main runs `bazel run //bazel/images:push_all`, which republishes
each chart at whatever version its `Chart.yaml` holds, with image tags stamped
`YYYY.MM.DD.HH.MM.SS-shortsha` pinned in at package time. Two failure modes
followed, both measured in the 2026-07-01 to 07-04 window (296 commits, 208
merged PRs):

1. **In-place mutation of a published version.** A code merge without a chart
   bump re-pushed the SAME chart version with different image tags. ArgoCD's
   repo-server serves its cached copy for sync (operation Succeeded) while the
   diff compares against the mutated re-pull: the app sits at sync=OutOfSync +
   operationState=Succeeded forever. 8 trailing bump-only PRs in the window
   exist purely to un-wedge this.
2. **Silent non-deploys.** When the PR-branch chart-version-bot skipped a
   needed bump (dependency-closure query under-scoping, 6 cases) or a
   rebase-merge dropped a duplicate bump commit (2 documented incidents), the
   merged code sat on main deploying nothing, discovered only by debugging the
   cluster. 20 of 208 PRs (9.6%) were pure 2-file bump PRs; one PR needed 5
   rebase+rebump+force-push cycles over 68 minutes because concurrent sessions
   raced version numbers.

## Decision

Two changes to the main-branch path of `bazel/helm/push.sh.tpl`, plus a helper:

1. **Publish is idempotent.** Before `helm push`, check whether the chart's
   current version already exists in the OCI registry (`helm show chart
oci://... --version`). If it exists, skip the push: a published chart
   version is immutable. If the existence probe errors for any other reason
   than "not found", fall through to the legacy push (a registry flake must
   not block releases).
2. **Missed bumps fail loudly.** When the push is skipped, run the existing
   `chart-version.sh` conventional-commit analysis over the chart's Bazel
   dependency closure. If it computes a version different from the published
   one, releasable commits exist that will never deploy: fail the `Push
images` action with a message naming the chart, both versions, and the
   exact fix command. A missed bump becomes a red main CI run minutes after
   merge instead of a cluster mystery hours later.
3. **Bumping is one race-free command.** `bazel/tools/git/bump-chart.sh`
   updates `Chart.yaml` and the application's semver `targetRevision`
   together, computing the next number from the origin/main tip (not the
   local checkout) and probing the registry for collisions, so concurrent
   sessions cannot pick the same number and rebase-merge cannot silently drop
   an "identical" bump.

## Alternatives considered

- **Content-digest comparison instead of version-existence.** Rejected:
  stamped image tags make every repackage byte-different, so "did the chart
  content change" is unanswerable from the artifact. Version existence is the
  only stable key.
- **Auto-bump on main (bot commit or bot PR) when the detector fires.** This
  would remove the remaining manual step entirely, but a bot writing to main
  (or auto-merging its own PRs) is a policy change with its own failure modes
  (racing human bumps, loops, branch protection). ADR 009 covers the full
  post-merge versioning design (Kargo promotion). Left open deliberately;
  the loud failure plus a one-command fix is the simplest thing that removes
  the silent failure class.
- **Semver-range `targetRevision` (e.g. `0.*`) so ArgoCD tracks the newest
  published chart.** Removes bump commits entirely but changes GitOps
  semantics: git would no longer pin what runs, and rollback becomes a
  registry operation instead of a revert. Not taken without an explicit
  decision.

## Consequences

- A bumpless merge that needed a bump now blocks the `Push images` action
  (including pushes for sibling charts later in the multirun) until a bump PR
  lands. This is intentional: the blocking window is minutes, visible, and
  actionable; the previous behavior was silent and unbounded.
- Chart versions in the registry are now immutable in practice as well as in
  intent; ArgoCD's cache behavior can no longer disagree with a re-pull.
- Bump-only PRs still exist (by design, git remains the source of truth for
  what deploys), but they are one command to produce and cannot collide.
