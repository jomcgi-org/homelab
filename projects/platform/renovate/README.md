# Renovate

The GKE hub is the only enrolled deployment for Renovate and apko lock
maintenance in `monolith-workflows`. The hub Application loads `values.yaml`
followed by `values-gke.yaml`, which configures both effective `spec.suspend`
values as `false`. Issue #7001 owns this activation; #7068 removed the retired
home enrollment. Live rollout and scheduled-run evidence remain on those
issues until observed.

## GitHub credential

`templates/onepassworditem.yaml` declares a `OnePasswordItem` named
`renovate-github` at `vaults/k8s-homelab/items/renovate-github`. Both
CronWorkflow templates consume the `RENOVATE_TOKEN` key of the
`renovate-github` Secret through `secretKeyRef`. The apko script uses that token
for Git and the GitHub API. There is no App-token minting implementation here.

The intended, unverified repository permission contract for the dedicated
identity is:

- Contents: read and write
- Commit statuses: read and write
- Issues: read and write
- Pull requests: read and write
- Workflows: read and write

Read-only organization member access is also required. Workflows write lets
the GitHub Actions manager update checked-in workflows.

The Renovate template and apko script still target `jomcgi/homelab`; the hub
Application reads configuration from `jomcgi-org/homelab`. Before activation
lands, verify Secret materialization and the dedicated
GitHub bot/App identity's intended repository access without disclosing Secret
contents. The configured repository name and credential references are
unchanged; this activation does not establish credential validity.

## Operations

The configured Renovate schedule is 04:00 America/Vancouver every day.
`renovate.json` limits ordinary PR creation to before 06:00 on Monday in the
same timezone; vulnerability alerts have an any-time exception. Concurrent runs
are forbidden, each run has a two-hour deadline, failed runs retry twice, successful
pods are removed promptly, and failed workflow state remains available for one
day.

The main Renovate scan requests and limits memory at 4 GiB. Scans of more than
500 extracted dependencies were repeatedly OOM-killed at a 2 GiB limit during
registry metadata resolution.

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
`projects/monolith/factory/orchestration/FACTORY.md`. Enabling scheduled
maintenance does not enable factory dependency landing or change its approval
requirements.

## Repository checks

`hub_deployment_test.py` follows the configured hub root to the Renovate
Application, checks the values overlay order, and renders both effective hub
suspension flags and the existing credential references. The base render with
`values.yaml` remains a build input without a deployment enrollment.
`dependency_policy_test.py` preserves chart exclusions, upstream eligibility,
manager labels, and the unconditional auto-merge prohibitions.

Linux renders and CI provide repository evidence only.

## Activation evidence

Before merging the activation, record timestamped read-only checks on the hub.
These commands print resource metadata and status, never Secret data:

```bash
date -u +%FT%TZ
kubectl get application renovate -n argocd -o 'custom-columns=NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status,REVISION:.status.sync.revision,RECONCILED:.status.reconciledAt'
kubectl get onepassworditem renovate-github -n monolith-workflows -o 'jsonpath={.metadata.name}{"\n"}{.status}{"\n"}'
kubectl get secret renovate-github -n monolith-workflows -o 'custom-columns=NAME:.metadata.name,CREATED:.metadata.creationTimestamp,VERSION:.metadata.resourceVersion'
kubectl get cronworkflow renovate renovate-apko-lock-maintenance -n monolith-workflows -o 'custom-columns=NAME:.metadata.name,SUSPEND:.spec.suspend,LAST:.status.lastScheduledTime,ACTIVE:.status.active[*].name'
kubectl get workflows -A -l 'workflows.argoproj.io/cron-workflow in (renovate,renovate-apko-lock-maintenance)' -o 'custom-columns=NAMESPACE:.metadata.namespace,NAME:.metadata.name,PHASE:.status.phase,START:.status.startedAt,FINISH:.status.finishedAt'
```

The Application must be healthy, the OnePasswordItem synchronized, the Secret
materialized, and both schedules suspended before the activation merges.
Confirm the dedicated identity has the documented permissions for the intended
repository without printing its token. Check current enrollment roots for a
competing writer and wait for any active maintenance Workflow to finish.
Record the available workflow-history interval. Successful workflows expire
after one hour and failed workflows after one day; an empty current list does
not prove no earlier run occurred. Missing evidence keeps activation blocked.

After reconciliation, repeat the resource checks and require both effective
`spec.suspend` values to be `false`. Observe successful scheduled runs of both
workflows, record their identities and timestamps, and link the resulting
dependency PRs under the dedicated identity. Ordinary Renovate PR creation
must occur within the configured Monday window; apko maintains
`renovate/apko-lock-maintenance`. Verify neither path requests auto-merge.
Keep #7001 and #7068 open until those operational checks are recorded.

For rollback, set both suspension fields in `values-gke.yaml` to `true` in a
reviewed PR and verify reconciliation. Suspending does not terminate a Workflow
already running. Wait for active work to finish before starting another writer.

## apko lock maintenance

A second CronWorkflow is configured for 01:00 America/Vancouver each Monday.
It regenerates every committed `apko.lock.json` on Linux through the repository's pinned
`rules_apko` toolchain, runs the committed-artifact generators, and maintains a
single `renovate/apko-lock-maintenance` PR. The script opens or updates the PR
and labels it `dependencies`; it never requests auto-merge. Updated Wolfi
packages require the same adversarial approval and separately authorized merge
as other dependency updates.
