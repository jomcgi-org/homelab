# Contributing Guide

Common tasks for contributing to the homelab. `AGENTS.md` at the repo root has
the rules every change follows (worktrees, `ci`, merging, chart versions); this
page does not repeat them.

## Repository Structure

A GitOps monorepo: each service's chart and deploy configuration live next to
its source.

| Directory                | Purpose                                                              |
| ------------------------ | -------------------------------------------------------------------- |
| `projects/`              | Services, operators and websites, each with its `chart/` and `deploy/` |
| `projects/gke-apps/`     | The hub's Application pins for services                              |
| `projects/platform-gke/` | Hub platform components, tracking git HEAD                           |
| `projects/gke-cluster/`  | Hub root Applications                                                |
| `projects/platform/`     | Platform component charts and values                                 |
| `projects/home-cluster/` | Residual home-cluster configuration: do not deploy to it             |
| `bazel/`                 | Build infrastructure (Helm rules, tools, images)                     |
| `docs/`                  | Cross-domain documentation, agent procedures and runbooks            |

## Adding a New Service

1. Create the chart in `projects/<service>/chart/`, or depend on an upstream
   chart from `Chart.yaml`.
2. Create `projects/<service>/deploy/` for the multi-source pattern; copy
   `projects/monolith/deploy/` and adjust names.
3. Add a hub Application under `projects/gke-apps/<service>/` and list it in
   `projects/gke-apps/kustomization.yaml`. `ci regen` does not do this.
4. Add health checks and observability (`docs/observability.md`).
5. Render it with
   `helm template <service> projects/<service>/chart/ -f projects/<service>/deploy/values.yaml`,
   then open a PR. After merge, confirm the rollout as `AGENTS.md` describes.

`projects/platform/ARCHITECTURE.md` section 4 explains how charts are versioned
and promoted.

## Adding Python Dependencies

Add the package to `pyproject.toml`, then regenerate the layered lock with
`bazel run //bazel/requirements:runtime` and
`bazel run //bazel/requirements:requirements.all` (details in
`bazel/ARCHITECTURE.md`). Reference it as `@pip//<package>`.

## Dependency Updates

Renovate opens weekly dependency PRs. Patch and minor upgrades may merge
automatically after a three-day release age and all required checks pass. Major
upgrades stay isolated for changelog review. A sibling Argo CronWorkflow
refreshes the committed apko locks weekly under the same CI-gated auto-merge
policy.
