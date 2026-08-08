# shared

Shared assets used across multiple projects in this repo. Currently contains
one thing: the `homelab-library` Helm library chart.

## homelab-library

`helm/homelab-library/chart` is a [Helm library chart](https://helm.sh/docs/topics/library_charts/)
(type: `library`, version `0.5.0`). It provides named template partials that
individual service charts call with `include`. Library charts produce no
Kubernetes manifests on their own — they only supply reusable helpers.

### Templates

| Template                                                      | File                   | Renders                                               |
| ------------------------------------------------------------- | ---------------------- | ----------------------------------------------------- |
| `homelab.name` / `homelab.fullname` / `homelab.chart`         | `_helpers.tpl`         | Standard name/label helpers                           |
| `homelab.labels` / `homelab.selectorLabels`                   | `_helpers.tpl`         | Common + selector label blocks                        |
| `homelab.componentLabels` / `homelab.componentSelectorLabels` | `_helpers.tpl`         | Per-component labels (`app.kubernetes.io/component`)  |
| `homelab.serviceAccountName`                                  | `_helpers.tpl`         | Service account name resolution                       |
| `homelab.deployment`                                          | `_deployment.tpl`      | Complete `apps/v1` Deployment                         |
| `homelab.statefulset`                                         | `_statefulset.tpl`     | Complete `apps/v1` StatefulSet with a PVC             |
| `homelab.service`                                             | `_service.tpl`         | `v1` Service (single or multi-port)                   |
| `homelab.serviceaccount`                                      | `_serviceaccount.tpl`  | `v1` ServiceAccount (optional)                        |
| `homelab.imagepullsecret`                                     | `_imagepullsecret.tpl` | 1Password-backed GHCR pull secret (`OnePasswordItem`) |

### Convention

`homelab.deployment` and `homelab.statefulset` read all per-component config
from `.Values.<component>`. Consumer templates set up values on that key, then
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

Security defaults: every Deployment and StatefulSet mounts an `emptyDir` at
`/tmp` (required when containers run read-only), inherits
`podSecurityContext`/`securityContext` from the chart's global values, and
respects the `imagePullSecret.enabled` toggle for GHCR auth.

### Adding as a dependency

Reference the chart with a `file://` path from the consuming chart's
`Chart.yaml`:

```yaml
dependencies:
  - name: homelab-library
    version: "0.5.0"
    repository: "file://../../shared/helm/homelab-library/chart"
```

Adjust the relative path depth as needed (services nested one level deeper use
`../../../shared/...`).

### Consumers

| Project                     | Chart path                                 |
| --------------------------- | ------------------------------------------ |
| `mcp/context-forge-gateway` | `projects/mcp/context-forge-gateway/chart` |
| `monolith-public`           | `projects/monolith-public/chart`           |

### Bumping the version

When changing any template, bump `version` in `Chart.yaml` here and update the
`version` field in every consumer's `Chart.yaml` dependency block to match.
Run `helm dependency update <chart-dir>` in each consumer to regenerate its
`charts/` lockfile before committing.
