# Renovate

The GKE hub is the sole intended deployment for Renovate and apko lock
maintenance in `monolith-workflows`. Both schedules are presently suspended:
the hub Application loads `values.yaml` followed by `values-gke.yaml`, which
sets both effective `spec.suspend` values to `true`.

Issue #7068 removes the retired home enrollment and cutover scaffolding while
keeping the hub default-off. Credential and identity verification, enablement,
and scheduled-run acceptance remain outstanding. Enablement overlaps #7001
and needs separately authorized ownership reconciliation before either flag
changes. This repository cleanup does not establish that dependency updates run.

## GitHub credential

`templates/onepassworditem.yaml` declares a `OnePasswordItem` named
`renovate-github` at `vaults/k8s-homelab/items/renovate-github`. Both
CronWorkflow templates consume the `RENOVATE_TOKEN` key of the
`renovate-github` Secret through `secretKeyRef`. The apko script uses that token
for Git and the GitHub API. There is no App-token minting implementation here.

The Renovate template and apko script still target `jomcgi/homelab`; the hub
Application reads configuration from `jomcgi-org/homelab`. A separately
authorized operator must verify Secret materialization and the dedicated
GitHub bot/App identity's intended repository access without disclosing Secret
contents. This cleanup neither verifies nor changes those credentials or
external-account settings.

## Operations

The configured Renovate schedule is 04:00 America/Vancouver every day.
`renovate.json` limits ordinary PR creation to before 06:00 on Monday in the
same timezone; vulnerability alerts have an any-time exception. Both hub
schedules remain suspended. When enabled, concurrent runs are
forbidden, each run has a two-hour deadline, failed runs retry twice, successful
pods are removed promptly, and failed workflow state remains available for one
day.

The main Renovate scan requests and limits memory at 4 GiB.

Package lifecycle scripts and plugins are disabled. Two repository-owned
maintenance commands are explicitly allowlisted: wrapper chart version bumps
and regeneration of compiled Python requirement locks. Renovate targets only
`jomcgi/homelab`, requires the checked-in `renovate.json`, and does not perform
repository autodiscovery or onboarding.

Auto-merge and platform auto-merge are disabled for every update type in
`renovate.json`. Release-age checks and required Linux CI still apply. PR
creation grants no merge authority; dependency updates require adversarial
review and separately authorized merging.

Factory dependency intake has its own activation checklist in
`projects/monolith/factory/orchestration/FACTORY.md`. This cleanup does not
enable dependency landing or change its approval requirements.

## Repository checks

`hub_deployment_test.py` follows the configured hub root to the Renovate
Application, checks the values overlay order, and renders both effective hub
suspension flags and the existing credential references. The base render with
`values.yaml` remains a build input without a deployment enrollment.
`dependency_policy_test.py` preserves chart exclusions, upstream eligibility,
manager labels, and the unconditional auto-merge prohibitions.

Linux renders and CI provide repository evidence only. The delivery PR for
this cleanup stays draft and unmerged under the operator landing hold.

## Outstanding acceptance

Keep #7068 open until separately authorized operational work records:

- Ownership reconciliation with #7001, identifying which reviewed PR owns the
  suspension flip, and current evidence of no retired home or competing writer.
  Record active runs and any limits in available workflow history.
- Hub `renovate` Application health, `renovate-github` OnePasswordItem sync and
  Secret materialization, and dedicated GitHub identity verification without
  disclosing credentials.
- A subsequent reviewed enablement and verified rollout with both effective
  hub `spec.suspend` values `false` and no competing or overlapping writer.
- A successful scheduled Renovate scan and resulting dependency PR evidence
  under the dedicated identity during the configured Monday PR window.
- A successful scheduled apko maintenance run and its PR, with auto-merge
  still disabled. Record workflow identities, timestamps, and PR evidence for
  both scheduled runs.

Changing `suspend` does not terminate an already-running Workflow. No live
acceptance check is established by this cleanup.

## apko lock maintenance

A second CronWorkflow is configured for 01:00 America/Vancouver each Monday
and is also suspended on the hub. When enabled, it regenerates
every committed `apko.lock.json` on Linux through the repository's pinned
`rules_apko` toolchain, runs the committed-artifact generators, and maintains a
single `renovate/apko-lock-maintenance` PR. The script opens or updates the PR
and labels it `dependencies`; it never requests auto-merge. Updated Wolfi
packages require the same adversarial approval and separately authorized merge
as other dependency updates.
