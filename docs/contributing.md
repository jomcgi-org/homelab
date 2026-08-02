# Contributing Guide

This document covers common tasks and workflows for contributing to the homelab.

## Repository Structure

This is a GitOps monorepo where related code and deployment configuration live together.

| Directory                | Purpose                                                                  |
| ------------------------ | ------------------------------------------------------------------------ |
| `projects/`              | All services, operators, websites — colocated with deploy configs        |
| `projects/platform/`     | Cluster-critical infrastructure (ArgoCD, Cilium, SigNoz, etc.)          |
| `projects/home-cluster/` | Auto-generated root kustomization that discovers all deploy/ directories |
| `bazel/`                 | Build infrastructure (Helm rules, tools, images, semgrep, wrangler)      |
| `docs/`                  | Design docs, ADRs, and runbooks                                          |

**Colocation principle:** Each service's deployment configuration (ArgoCD Application, Helm values) lives next to its source code, not in a separate overlays directory. This makes it easy to understand what belongs together.

## Adding a New Service

### ArgoCD Discovery Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│  Step 1: Create Helm Chart (if needed)                              │
│  projects/<service>/chart/  (custom chart)                          │
│  — or use an upstream chart via Chart.yaml dependencies             │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Step 2: Create deploy/ Directory                                   │
│  projects/<service>/deploy/                                         │
│    ├── application.yaml    (ArgoCD Application manifest)            │
│    ├── kustomization.yaml  (makes app discoverable)                 │
│    └── values.yaml         (Helm value overrides)                   │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Step 3: Auto-Discovery                                             │
│  Run: format                                                        │
│  This runs bazel/images/generate-home-cluster.sh which scans        │
│  projects/ for deploy/ dirs containing application.yaml and         │
│  regenerates projects/home-cluster/kustomization.yaml               │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Step 4: ArgoCD Auto-Discovery                                      │
│  projects/home-cluster/kustomization.yaml lists all deploy/ dirs.   │
│                                                                     │
│  The "canada" Application is the root app-of-apps.                  │
│  ArgoCD runs "kustomize build" and discovers all                    │
│  Application manifests automatically.                               │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Step 5: ArgoCD Syncs Application                                   │
│  - Renders Helm chart with value files                              │
│  - Applies manifests to cluster                                     │
│  - Monitors health and sync status                                  │
└─────────────────────────────────────────────────────────────────────┘
```

### Service Directory Structure

```
projects/
├── platform/              # Core infrastructure (flat layout: chart + deploy in same dir)
│   ├── argocd/
│   │   ├── Chart.yaml
│   │   ├── application.yaml
│   │   ├── kustomization.yaml
│   │   └── values.yaml
│   ├── cilium/
│   ├── kyverno/
│   └── kustomization.yaml  ← references all platform services
│
├── monolith/               # Colocated service
│   ├── chart/
│   ├── deploy/
│   │   ├── application.yaml
│   │   ├── kustomization.yaml
│   │   └── values.yaml
│   └── backend/            # Source code lives alongside
│
├── grimoire/              # Individual project with colocated deploy/
│   ├── chart/             # Custom Helm chart
│   ├── deploy/            # ArgoCD Application + values
│   │   ├── application.yaml
│   │   ├── kustomization.yaml
│   │   └── values.yaml
│   └── api/               # Source code lives alongside
│
└── home-cluster/          # Auto-generated root (DO NOT EDIT)
    └── kustomization.yaml  ← lists all deploy/ dirs
```

### Steps

1. Create Helm chart in `projects/<service>/chart/` with default values (or use an upstream chart via Chart.yaml dependencies)
2. Create a `deploy/` directory colocated with your service source:
   - `projects/<service>/deploy/application.yaml` - ArgoCD Application pointing to your chart
   - `projects/<service>/deploy/kustomization.yaml` - Reference to application.yaml
   - `projects/<service>/deploy/values.yaml` - Helm value overrides
3. Run `format` to regenerate the root kustomization and format code
4. Add health checks and observability to the chart
5. Test the complete deployment path:
   - `helm template <service> projects/<service>/chart/ --namespace <namespace>` to verify rendering
   - Commit and push to Git
   - ArgoCD automatically discovers and syncs the new application to the cluster

## Format Command

Run before committing changes:

```bash
format
```

This command:

- **Formats code** (Go, Python, JavaScript, Shell, Starlark)
- **Updates apko lock files** (container image definitions)
- **Updates Python lock files** (from pyproject.toml)
- **Validates apko configurations**
- **Regenerates `projects/home-cluster/kustomization.yaml`**
- **Runs in parallel** using Bazel for fast builds

Note: Helm manifests are rendered by ArgoCD at deploy time, not committed to the repo.

## Adding Python Dependencies

When adding a new Python dependency to `pyproject.toml`:

```bash
# 1. Add dependency to pyproject.toml
# 2. Regenerate lock files (included in format command)
format
```

## Development Workflow

1. **Make changes** in feature branch (via worktree)
2. **Run `format`** to format code and update lock files
3. **Verify deployment** works end-to-end
4. **Check observability** - metrics, logs, traces
5. **Create PR** - BuildBuddy CI runs format check + `bazel test //...`
6. **Merge** - ArgoCD automatically syncs changes to production cluster

## Merge Discipline

All changes land by rebase merge; squash and merge commits are disabled.
Required checks are strict: a branch must be up to date with main before it
can merge, so every open PR re-runs CI against post-merge main. That re-run
is what makes chart version collisions between concurrent PRs detectable
(see [ADR platform/011](decisions/platform/011-idempotent-chart-publish-missed-bump-detection.md)).
Any PR whose code must deploy bumps its chart version in the same PR; a
missed bump fails the main-branch image push loudly with the fix command.
Renovate opens weekly PRs for upstream chart drift; upgrades are judged
against changelogs before merge, never auto-merged.

## Testing Philosophy

We test **actual behavior**, not implementation details:

**Good Tests:**

- Deploy the actual service to a test cluster
- Verify the service responds correctly via HTTP
- Confirm metrics are exported and observable
- Test the complete user journey

**Bad Tests:**

- Unit tests that mock everything
- Tests that verify internal implementation
- Tests that don't exercise real deployment paths
