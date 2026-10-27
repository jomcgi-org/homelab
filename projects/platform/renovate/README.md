# Renovate

Renovate runs as a daily Argo `CronWorkflow` in `monolith-workflows`. The home
cluster remains the active writer while the GKE hub deployment is staged. The
hub Application loads `values.yaml` and then `values-gke.yaml`, which renders
both the Renovate scan and apko lock maintenance CronWorkflows with
`spec.suspend: true`. Merging that enrollment creates no second scheduled
writer. The home Application still loads only `values.yaml`, so its existing
writers remain active until the operator-controlled cutover.

The repository configuration keeps ordinary dependency PR creation inside the
Monday maintenance window. Running the workflow daily means one transient
failure does not delay updates for a full week.

## GitHub credential

Prefer the existing `renovate-github` item in the `k8s-homelab` 1Password vault
and its `RENOVATE_TOKEN` field. Verify the dedicated bot/App identity and access
to `jomcgi-org/homelab` without exposing the token. A new token, installation or
permission grant needs exact confirmation before provisioning.

The token needs these repository permissions:

- Contents: read and write
- Commit statuses: read and write
- Issues: read and write
- Pull requests: read and write
- Workflows: read and write

It also needs read-only organization member access. Workflow permission lets the
GitHub Actions manager update checked-in workflows.

The 1Password Operator materializes the item as the `renovate-github` Secret.
Renovate reads only its `RENOVATE_TOKEN` field.

## Operations

The schedule is 04:00 America/Vancouver every day. Concurrent runs are
forbidden, each run has a two-hour deadline, failed runs retry twice, successful
pods are removed promptly, and failed workflow state remains available for one
day.

The main Renovate scan requests and limits memory at 4 GiB. The repository has
more than 500 extracted dependencies, and a 2 GiB limit was repeatedly killed
during registry metadata resolution.

Package lifecycle scripts and plugins are disabled. The sole allowlisted
maintenance command regenerates the root compiled Python requirement locks.
Helm updates refresh lock files and vendored archives without advancing wrapper
chart versions; main publication owns those versions. Renovate targets only
`jomcgi-org/homelab`, requires the checked-in `renovate.json`, and does not perform
repository autodiscovery or onboarding.

The proposed repository policy enables platform PR auto-merge for ordinary
major, minor, patch, digest, pin and lock updates. GitHub's existing merge queue
owns merging; no approval review is manufactured. Ordinary PR creation remains
before 06:00 Monday in America/Vancouver, with five concurrent PRs, two per hour
and a three-day release age. Security alerts retain their immediate exception.
See [coverage and holds](COVERAGE.md) for exact exclusions and generated files.

**Activation is held.** Do not land this policy or enable a writer until
`factory/dependency-evidence` is required in ruleset 9180009, pinned to its
verified App integration, alongside `pr-checks`. Renovate's native auto-merge
can enqueue before checks finish: the ruleset must enforce independent review.
The staged publisher must have a verified existing identity, fresh PR/queue
canaries, stale-evidence refusal and the CI credential boundary from #7005.
No credential, App grant, ruleset or live intake setting is changed by this PR.
Reusing an existing installation is preferred; a new grant needs exact approval.

Both independent dependency assessments must record `authorization_scope`.
Ordinary bumps, including majors, need no routine human approval. Credential,
access, security-policy, budget or audience changes bundled with an update
need exact existing authority or the review refuses that update. Existing holds
and compatibility pins remain binding. Earlier assessments without this new
dimension cannot pass the evidence gate.

The factory's dependency landing function still refuses to arm auto-merge.
Renovate would arm its own PRs once activated; the factory supplies independent
evidence. The separate apko job still creates PRs without requesting auto-merge.
Empty GitHub dependency-graph comparisons are refused by the current evidence
gate, so unsupported ecosystems need an attested evidence path before they can
merge automatically. These are outstanding activation gaps, not passing evidence.

#7168 owns retired-home cleanup and #7186 owns the hub suspension flip under
#7001/#7068. Reuse those PRs; neither schedule is changed here. Keep those issues
open until successful scheduled scans and dedicated-bot PRs are observed.

## GKE staged cutover

Repository preflight is non-writing. Render the home chart with `values.yaml`,
the hub chart with `values.yaml` followed by `values-gke.yaml`, and the future
home cutover state with `values.yaml` followed by
`values-home-suspend.yaml`. The focused `migration_staging_test.py` asserts the
effective `spec.suspend` values, not just the overlay filenames. An operator may
also inspect the hub Application, its suspended CronWorkflows, and the existing
`OnePasswordItem` and Secret materialization. Those checks do not establish
that either writer can complete its GitHub work.

The live handoff is deliberately separate from this staged merge:

1. Confirm the hub Application is healthy, both hub CronWorkflows are suspended,
   and the home CronWorkflows remain active. Inspect both clusters for active
   Workflows and wait for every run to finish. Setting `suspend` does not stop a
   Workflow that is already running.
2. During the staged overlap, audit both clusters' workflow history and confirm
   that no hub writer fired. The hub's effective suspended values make this a
   bounded staging check, not evidence that its GitHub writes work and not a
   claim of exactly-once execution.
3. At the operator-controlled cutover, make a reviewed repository change that
   adds `values-home-suspend.yaml` after `values.yaml` in the home
   `application.yaml`. Wait for the home Application to reconcile, wait for any
   already-active home Workflow to finish, and verify both home CronWorkflows
   remain suspended. Keep both hub CronWorkflows suspended throughout this step.
4. Only after the home suspension is effective, make a separate reviewed change
   that sets both fields in `values-gke.yaml` to `false`. After the hub
   reconciles, wait for the scheduled hub Renovate scan to complete and surface
   its result and for scheduled hub apko maintenance to complete and open or
   update its maintenance PR. Manual rendering, sync, or suspension checks are
   not substitutes for those scheduled writing checks. Keep Renovate dashboard
   issue #4493 open.

If hub verification fails, restore both hub fields to `true` and wait for any
active hub Workflow to finish before removing the home overlay in another
reviewed change. This ordering always establishes one suspended side before
enabling the other; it does not terminate in-flight work.

Issue #6247 retains the operational acceptance for the scheduled hub scan, the
scheduled apko maintenance PR, and the duplicate-writer audit plus durable home
suspension. The home enrollment stays in place until that acceptance is complete
and any later home-cluster retirement is separately authorized.

## apko lock maintenance

A second CronWorkflow runs at 01:00 America/Vancouver each Monday. It regenerates
every committed `apko.lock.json` on Linux through the repository's pinned
`rules_apko` toolchain, runs the committed-artifact generators, and maintains a
single `renovate/apko-lock-maintenance` PR. The script opens or updates the PR
and labels it `dependencies`; it never requests auto-merge. Updated Wolfi
packages require the same adversarial approval and separately authorized merge
as other dependency updates.
