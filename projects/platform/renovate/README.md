# Renovate

The GKE hub is the sole intended deployment for Renovate and dependency lock
maintenance in `monolith-workflows`. Its Application loads `values.yaml` then
`values-gke.yaml`; both effective CronWorkflow suspension flags remain `true`.
The base render is a build input, not another deployed writer.

PR #7358 integrates #7168's existing cleanup commit and resolves its README and
renamed policy-test conflicts. The retired home enrollment and cutover files
stay removed; #7168's branch is not rewritten. #7186 remains the existing draft
that owns the two suspension flips under #7001/#7068. No PR has been merged and
no schedule, live flag or credential has been activated by this integration.

## GitHub credential

Both CronWorkflows reference `renovate-github` / `RENOVATE_TOKEN`, supplied by the
existing OnePasswordItem at `vaults/k8s-homelab/items/renovate-github`. Prefer
that existing identity and installation. The workflow templates target only
`jomcgi-org/homelab`. There is no App-token minting or refresh implementation in
this chart; the credential's actual type, expiry/renewal and usable permissions
remain unverified. Do not infer these from a synced Application or the item name.

An authorized owner must verify the installed identity's existing repository
selection and grants before proposing any missing scope. Renovate's
[GitHub authentication contract](https://docs.renovatebot.com/modules/platform/github/#running-as-a-github-app)
differs for App and fine-grained tokens. In particular, managed workflow-file
updates need workflow-write capability and App enqueue operations need merge
queue write capability. The independent evidence publisher needs its own
verified App identity for repository/PR, dependency graph, open-alert, commit-status and queue
reads plus check publication. These are capability checks, not permission grants.
Any new token, installation, scope, binding or renewal setup needs a concrete
separately approved change. No Secret object or value is read in this task.

## Operations

The schedule is 04:00 America/Vancouver every day. Concurrent runs are
forbidden, each run has a two-hour deadline, failed runs retry twice, successful
pods are removed promptly, and failed workflow state remains available for one
day.

The main Renovate scan requests and limits memory at 4 GiB. The repository has
more than 500 extracted dependencies, and a 2 GiB limit was repeatedly killed
during registry metadata resolution.

Package lifecycle scripts and plugins are disabled. The sole allowlisted
post-upgrade command regenerates the root compiled Python requirement locks.
The existing separate weekly job also refreshes apko/root Python locks and
non-GitHub tool pins; it does not expand Renovate's command allowlist.
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

Both independent dependency assessments must record `authorization_scope` and
`evidence_coverage`, and list the complete server-fetched changed-file inventory
in `reviewed_files`. Ordinary bumps, including majors, need no routine human
approval. Credential, access, security-policy, budget or audience changes
bundled with an update need exact existing authority or review refuses that
update. Existing holds and compatibility pins remain binding. Old assessments
without these dimensions cannot pass.

The factory's own landing function remains inspection-only for dependency
PRs. Renovate and the weekly maintenance job request native platform auto-merge;
the factory supplies independent evidence, and the required App-bound queue
check must enforce it. A successful empty GitHub graph is a coverage limitation:
reviewers must provide upstream advisory/artifact evidence. Both reviewers must
agree on complete base/head `advisory_sources` inventories bound to the reviewed
commits and covering every corresponding changed file. Each component records a
stable occurrence identity, immutable artifact hash, exposure fingerprint, exact
package version or Git commit, file associations and full OSV response hash.
Completion and each publication freshly query both inventories without credentials.
Partial remediation is eligible when unrelated findings remain only on exactly
unchanged components. Added vulnerable versions, new vulnerable occurrences or
exposure, and changed advisory records invalidate approval. A new version that
still has findings refuses even if it removes others. This preserves the
established non-regression policy; it creates no vulnerability waiver.

The server enforces inventory agreement, commit/file binding and response
comparison. Both reviewers must establish the complete dependency/SBOM closure,
exposure inputs and feed applicability; hashes or unknown package names returning
empty results do not prove coverage. Unsupported coverage, incomplete inventories,
pagination and read failures refuse the update. The bound is 100 components per
side; larger inventories still need a supported adapter.

Dependency checks wait for exact-head `pr-checks`, then re-read evidence and the
durable approval immediately before each publication, within a 60-second
collection budget. Slow reads revoke an existing success while the publisher
can write. This bounds collection age, not the lifetime of a check at GitHub:
[GitHub's Checks API](https://docs.github.com/en/rest/checks/runs?apiVersion=2022-11-28)
has no expiry field. This is point-in-time verification of the exact PR or queue
commit, not atomic merge-time freshness. A dead publisher or lost token cannot
revoke a stored success, and an advisory may arrive between publication and merge.
The activation canary must demonstrate the implemented checks and refusals, and
Joe must explicitly accept this residual race before enabling automatic merges.
An expiry service is not an existing repository requirement or a prerequisite
introduced by this change.

#7168 owns retired-home cleanup and #7186 owns the hub suspension flip under
#7001/#7068. Reuse those PRs; neither schedule is changed here. Keep those issues
open until successful scheduled scans and dedicated-bot PRs are observed.

## Repository checks

`hub_deployment_test.py` is retained from #7168: it follows the actual hub root,
checks overlay order, and verifies effective suspension, resource identities,
schedules and credential references. `dependency_policy_test.py` retains the
expanded extraction, maintenance, ownership and reviewed auto-merge tests from
#7358 under the cleanup's new filename. Each Bazel target collects only its own
test file. Policy tests retain the Helm and Node runfiles needed to exercise the
rendered maintenance scripts. The retired two-cluster tests are not restored.

These checks establish repository behavior, not live credential or rollout
acceptance. Changing `suspend` does not terminate an already-running Workflow.

## Weekly dependency maintenance

The existing second CronWorkflow runs at 01:00 America/Vancouver each Monday.
It keeps separate `renovate/<mode>-lock-maintenance` PRs for `apko`, `python`,
`atlas`, `helm`, `op` and `claude`. Apko uses pinned `rules_apko` plus generators.
Python runs both root requirement compilers with `--upgrade --no-build`.
The shared compiler also refuses source builds, and Renovate passes
`UV_NO_BUILD=true` to native uv artifact commands. Packages that require build-backend execution need isolated generation; a
required source build is a failed automatic update. Each tool adapter
updates all platforms atomically, without executing downloaded code. Each mode
fails independently, and existing held, assigned, draft or human-authored PRs
are preserved. The job labels PRs `dependencies` and requests REBASE platform
auto-merge; it never submits an approval or bypasses checks. These requests
require the same App-bound evidence gate and CI as ordinary Renovate updates.

## Smallest owner handoff

The following steps remain on #7001/#7068 and #7005. They distinguish an action
an authorized operator must be able to perform from approval to change settings.
A statement of approval cannot replace missing access, evidence or passing CI.

| Step | Kind | Exact target and evidence |
| --- | --- | --- |
| Verify existing identities | Manual/read-only capability | Joe or an authorized GitHub owner records the installed Renovate App/installation IDs, bot login and numeric user ID, selected `jomcgi-org/homelab` repository, actual grants and credential renewal arrangement; separately identify the trusted check publisher's numeric App ID. The organization-installation API returned HTTP 403 and is not retried through another route. No new grant is needed merely to report these facts. |
| Verify the suspended hub | Manual/read-only capability | An authorized operator records the effective `renovate` and `renovate-apko-lock-maintenance` CronWorkflow states, available active-run/history evidence and its retention limits, plus OnePasswordItem synchronization/materialization attestation without retrieving Secret objects or values. Current tools cannot supply these resource reads. |
| Close implementation/evidence gaps | Platform engineering and validation | #7005 still requires pre-execution isolation of same-repository PR and queue credentials and snapshots. Verify the concrete provider target and budget/access implications before authorizing its implementation. Complete base/head OSV revalidation and bounded late publication are implemented here; verify live feed applicability and point-in-time queue behavior. Unsupported advisory coverage still refuses the particular update. Any required token renewal/wiring is designed only after the existing identity is known. |
| Authorize the verified canary setup | Exact settings approval | Only after identities and isolation are verified: supply the existing trusted publisher identity as `FACTORY_DEPENDENCY_GATE_TOKEN` and its matching `FACTORY_DEPENDENCY_GATE_APP_ID`, enable `FACTORY_DEPENDENCY_GATE_ENABLED`, and set `intake.dependency_pr_authors` / `dependency_pr_author_ids` to the verified dedicated bot, never `jomcgi`. Propose any missing grant/binding separately, scoped to this repository. Keep both scheduled writers suspended while testing PR and single-entry queue success, unapproved/unsafe/stale evidence, unavailable reads and late ownership holds. Verify publisher/token loss and an advisory arriving after success, and explicitly accept the remaining post-publication race; GitHub does not expire that success. |
| Authorize enforcement and release | Exact settings and PR approval | After canaries pass, require `factory/dependency-evidence` from the verified App integration in ruleset `9180009`, preserving `pr-checks`, the queue and all protections. Release the reviewed cleanup/policy work in the existing PRs, then rebase #7186 onto the integrated result and refresh its CI/review before approving its two suspension flips. The earlier #7168 enqueue denial requires exact release authorization; no enqueue is retried here. |

The observed App IDs and bot identity must appear in the concrete approval
request; they are not guessed here. Verification alone does not enable anything.
After authorized release, observe the hub Application and effective schedules,
then successful scheduled scans, all maintenance modes, dedicated-bot PR authors,
independent evidence and queue outcomes before closing #7001/#7068. Reuse #7186
for activation; do not create another activation task or change live resources.

## Credential isolation boundary (#7005)

Read-only ownership check on 2026-10-27: #7005 is open, `needs-thought`, with no
assignee, comments or factory receipt; PR search found no separate implementation.
This work reuses that issue and does not claim the boundary is resolved.

BuildBuddy [injects secrets into trusted workflows](https://www.buildbuddy.io/docs/secrets/).
Its [workflow service](https://github.com/buildbuddy-io/buildbuddy/blob/master/enterprise/server/workflow/service/service.go)
classifies same-repository events as trusted, reads their workflow configuration
from the pushed commit, and supplies runner repository credentials. The
[action schema](https://github.com/buildbuddy-io/buildbuddy/blob/master/enterprise/server/workflow/config/config.go)
has no per-event secret exclusion. The hosted deployment/version and effective
credential capabilities still require owner verification; these upstream facts
are not a read of live credentials.

A shell `unset`, an early assertion or splitting actions in PR-editable YAML
cannot enforce this boundary. The current `pr-checks` also inherits main's
snapshot namespace, so merely withholding new environment variables does not
prove old Docker/git credentials are absent. #7005 needs a platform-enforced
untrusted PR/queue execution boundary, isolated snapshots/cache authority and a
separate main-only publication identity. Preserve the required check name and
main publication, including generated-file handling, in that migration. Changing
provider configuration, token capabilities, cache/snapshot state or publication
wiring is outside this code-only pass and needs an exact verified proposal.
No workflow split or credential change is presented as an implemented safeguard.
