# Renovate

Renovate runs as a daily Argo `CronWorkflow` in `monolith-workflows`. The
repository configuration keeps ordinary dependency PR creation inside the
Monday maintenance window. Running the workflow daily means one transient
failure does not delay updates for a full week.

## GitHub credential

Before merging this deployment, create a secure-note item named
`renovate-github` in the `k8s-homelab` 1Password vault. Add a concealed field
named `RENOVATE_TOKEN` containing a fine-grained token for the dedicated bot
identity, scoped only to `jomcgi/homelab`.

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

Package lifecycle scripts and plugins are disabled. Two repository-owned
maintenance commands are explicitly allowlisted: wrapper chart version bumps
and regeneration of compiled Python requirement locks. Renovate targets only
`jomcgi/homelab`, requires the checked-in `renovate.json`, and does not perform
repository autodiscovery or onboarding.

Patch and minor upgrades request GitHub auto-merge after the three-day release
age and required CI checks pass. Major upgrades remain separate and require
human review.

## apko lock maintenance

A second CronWorkflow runs at 01:00 America/Vancouver each Monday. It regenerates
every committed `apko.lock.json` on Linux through the repository's pinned
`rules_apko` toolchain, runs the committed-artifact generators, and maintains a
single `renovate/apko-lock-maintenance` PR. That PR requests rebase auto-merge,
so the same required CI checks gate updated Wolfi packages before they land.
