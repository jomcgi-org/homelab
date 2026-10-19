# shared

Shared assets used across multiple projects in this repo. Currently contains
one thing: the `homelab-library` Helm library chart.

## homelab-library

`helm/homelab-library/chart` is a [Helm library chart](https://helm.sh/docs/topics/library_charts/)
(type: `library`, version `0.7.0`). It provides named template partials that
individual service charts call with `include`. Library charts produce no
Kubernetes manifests on their own; they only supply reusable helpers.

### Templates

| Template                                                      | File                   | Renders                                               |
| ------------------------------------------------------------- | ---------------------- | ----------------------------------------------------- |
| `homelab.name` / `homelab.fullname` / `homelab.chart`         | `_helpers.tpl`         | Standard name/label helpers                           |
| `homelab.labels` / `homelab.selectorLabels`                   | `_helpers.tpl`         | Common + selector label blocks                        |
| `homelab.componentLabels` / `homelab.componentSelectorLabels` | `_helpers.tpl`         | Per-component labels (`app.kubernetes.io/component`)  |
| `homelab.serviceAccountName`                                  | `_helpers.tpl`         | Service account name resolution                       |
| `homelab.imageRef`                                            | `_helpers.tpl`         | Image reference, preferring the digest over the tag   |
| `homelab.deployment`                                          | `_deployment.tpl`      | Complete `apps/v1` Deployment                         |
| `homelab.service`                                             | `_service.tpl`         | `v1` Service (single or multi-port)                   |
| `homelab.serviceaccount`                                      | `_serviceaccount.tpl`  | `v1` ServiceAccount (optional)                        |

### Convention

`homelab.deployment` reads all per-component config
from `.Values.<component>`. Consumer templates set values on that key, then
call:

```yaml
{ { - include "homelab.deployment" (dict "context" . "component" "api") } }
```

The optional `componentName` key lets the values key be camelCase while the
Kubernetes resource name stays kebab-case:

```yaml
{
  {
    - include "homelab.deployment" (dict "context" . "component" "wsGateway" "componentName" "ws-gateway"),
  },
}
```

Security defaults: every Deployment mounts an `emptyDir` at
`/tmp` (required when containers run read-only) and inherits
`podSecurityContext`/`securityContext` from the chart's global values. When
`imagePullSecret.enabled` is true the pod references a `ghcr-imagepull-secret`
Secret that must already exist in the namespace; the library does not create
it. Otherwise, `imagePullSecrets` is used verbatim when set.

### Adding as a dependency

Reference the chart with a `file://` path from the consuming chart's
`Chart.yaml`:

```yaml
dependencies:
  - name: homelab-library
    version: "0.7.0"
    repository: "file://../../shared/helm/homelab-library/chart"
```

Adjust the relative path depth as needed (services nested one level deeper use
`../../../shared/...`).

### Consumers

| Project                     | Chart path                                 |
| --------------------------- | ------------------------------------------ |
| `mcp/context-forge-gateway` | `projects/mcp/context-forge-gateway/chart` |
| `monolith-public`           | `projects/monolith-public/chart`           |
| `monolith-agents`           | `projects/monolith-agents/chart`           |

### Bumping the version

When changing any template, bump `version` in the library `Chart.yaml` and
update the `version` field in each consumer's `Chart.yaml` dependency block to
match (`projects/monolith-public/chart`, `projects/monolith-agents/chart`,
`projects/mcp/context-forge-gateway/chart`). Then regenerate each consumer's
vendored artifacts: its `Chart.lock` and `charts/homelab-library-<version>.tgz`
(`helm dependency update <chart-dir>` rebuilds both, and removes the old
tarball so exactly one remains).

`bazel/tools/format/sync-helm-deps.sh --check` flags version mismatches, and
`bazel/tools/format/helm_deps/check_helm_deps.py --root .` verifies each
committed tarball matches the library source (it compares `templates/*` and
`values.yaml` plus the `Chart.yaml` version); CI runs the latter. Note that
`projects/mcp/context-forge-gateway/chart` also depends on `mcp-stack` from
`oci://ghcr.io/jomcgi/homelab/charts`, so `helm dependency update` there needs
registry auth. Log in with your GitHub username and enter a personal access
token (classic) with `read:packages` access to the package at the password
prompt, then regenerate the dependencies:

```bash
helm registry login ghcr.io --username YOUR_GITHUB_USERNAME
helm dependency update projects/mcp/context-forge-gateway/chart
```

Let `helm dependency update` generate `Chart.lock`, including its dependency
digest. Do not hand-edit the lock file.
