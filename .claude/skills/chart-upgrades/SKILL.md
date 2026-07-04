---
name: chart-upgrades
description: Inventory the upstream charts and dependencies we deploy, research their changelogs, and judge whether each upgrade is worth taking. Use when asked "what's behind", "should we upgrade X", "any chart upgrades worth doing", or to triage the Renovate dependency dashboard. Mechanical drift detection is Renovate's job; this skill is the judgment layer.
---

# Chart Upgrade Advisor

Renovate detects version drift mechanically (see `renovate.json`). This skill does what Renovate cannot: read the actual changelog for each drifted dependency, weigh it against how this cluster uses the chart, and return a per-component verdict of **Upgrade now / Skip / Schedule** with reasoning.

Two halves: build the inventory of what we run, then research and judge each candidate.

## 1. Build the inventory (what we deploy)

There are exactly two regular shapes that declare an **upstream** (third-party) version. Glob both; ignore everything first-party.

- **ArgoCD app sources** in `projects/**/application.yaml`: a `sources[]` entry whose `repoURL` is NOT `github.com/jomcgi/homelab` and NOT `ghcr.io/jomcgi/homelab/charts` pins `chart` + `targetRevision` directly. Example: `cloudnative-pg @ 0.27.1`, `atlas-operator @ 0.7.28`.
- **Chart.yaml dependencies** in `projects/**/Chart.yaml`: a `dependencies[]` entry whose `repository` is `http(s)://` or `oci://` (NOT `file://`). Example: `argo-cd @ 8.5.3`, `cert-manager`, `gateway-helm`.

Quick pass to list them:

```bash
# upstream app-source pins
grep -rhA3 "sources:" projects --include=application.yaml \
  | grep -E "repoURL:|chart:|targetRevision:" | grep -v "jomcgi/homelab"
# upstream chart deps (skip file:// = first-party libraries)
grep -rhB1 -A2 "repository:" projects --include=Chart.yaml | grep -v "file://"
```

Normalize each into: `component | current_version | source_type (app-source | chart-dep) | upstream_repo | manifest_path`.

**Two traps that change which changelog you read:**

- **Chart version is not app version.** The `argo-cd` chart `8.5.3` ships Argo CD `2.13.x` (see its `appVersion`). The changelog you care about is almost always the **app's**, not the chart's. Always resolve `appVersion` before searching.
- **Wrapped charts hide the version one level down.** Most `projects/platform/*` apps point `repoURL` at our own git `HEAD` and wrap the upstream chart as a `Chart.yaml` dependency inside the service dir (longhorn, linkerd, signoz, keda, kyverno, coredns, nvidia-gpu-operator, otel-operator). The upstream version is the `dependencies[]` pin, not the `application.yaml`.

## 2. Get the candidate list (drift)

Prefer Renovate's output if it is active:

```bash
gh issue list --label dependencies --search "Upstream chart and dependency drift" --json number,title
gh pr list --label upstream-chart --json number,title,headRefName,body
```

Read the **Dependency Dashboard** issue: it lists every detected current-to-latest jump in one place. If Renovate is not active yet (the GitHub App is not installed and no self-hosted runner exists), fall back to researching the specific components the user names against the inventory from step 1.

## 3. Research each candidate

For each `current -> latest`:

1. **Find the changelog.** Map component to source: GitHub Releases (most operators and charts), ArtifactHub (chart-level notes), or the project's docs. Use WebFetch on the release notes between current and latest; for multi-version jumps read every intermediate major/minor, not just the endpoints.
2. **Read for impact, in this order of severity:**
   - Removed/changed APIs or CRD schema changes (the most common breakage for operators).
   - Renamed or restructured `values.yaml` keys: cross-check against our override file (`projects/<svc>/deploy/values.yaml` or the wrapper chart values). A renamed key we override silently goes back to its default.
   - RBAC additions the chart now requires.
   - Security fixes (these raise priority).
   - Default behavior changes.

## 4. Judge against THIS cluster

Repo-specific things that change the verdict:

- **CRD-bundled charts** carry two known hazards here: the 256 KiB `last-applied-configuration` annotation cap (a chart whose bundled CRDs grow past it breaks ArgoCD sync with `metadata.annotations: Too long`), and Semgrep Pro k8s rules false-positiving on CRD schema text (pre-exclude in the BUILD entry, the Pro engine is Linux-only so it cannot be reproduced locally).
- **Linkerd-meshed namespaces**: never let an upgrade introduce K8s NetworkPolicies there (port 4143 mismatch).
- **Image digests** are pinned by the Bazel pipeline. Never hand-pin `@sha256:` in values; let CI manage it.
- **No local test loop.** Verification is end-of-PR CI on the pushed branch.

## 5. Output and apply

Return a table plus a short rationale per row:

| Component      | Current -> Latest | Verdict     | Why                                        |
| -------------- | ----------------- | ----------- | ------------------------------------------ |
| cloudnative-pg | 0.27.1 -> X       | Upgrade now | Patch, no CRD/API change, fixes Y          |
| atlas-operator | 0.7.28 -> X       | Schedule    | Minor renames `values.foo`; we override it |

When applying an accepted upgrade:

- **Direct app-source chart** (e.g. cloudnative-pg, atlas-operator): bump `targetRevision` in that service's `application.yaml`. That is the whole change.
- **Wrapped upstream chart**: bump the `dependencies[].version` in the service `Chart.yaml` AND bump that wrapper chart's own `version` (a chart change requires a version bump), keeping `application.yaml` `targetRevision` in sync. `chart-version-bot` normally syncs the latter pair, but a manual bump must touch both.
- Always: worktree + PR, rebase merge, run `format`, push, watch CI. Never commit to main.

Let Renovate's PR carry the mechanical bump where one exists; this skill's value is the verdict and the cluster-specific caveats attached to it.
