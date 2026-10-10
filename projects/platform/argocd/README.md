# ArgoCD

GitOps continuous deployment controller. Syncs Kubernetes manifests from Git to cluster.

## Overview

Wrapper chart around the official [argo-cd](https://argoproj.github.io/argo-helm) Helm chart with local defaults.

```mermaid
flowchart LR
    Git[Git Repository] --> ArgoCD
    ArgoCD --> K8s[Kubernetes Cluster]
    ArgoCD -.->|watches| Git
```

## Key Features

- **Auto-sync** - Automatically applies changes pushed to Git
- **Self-healing** - Reverts manual cluster changes to match Git state
- **Application discovery** - Finds apps via Kustomize overlays

## Configuration

| Value       | Description           | Default                                                                             |
| ----------- | --------------------- | ----------------------------------------------------------------------------------- |
| `argo-cd.*` | Upstream chart values | See [argo-cd chart](https://github.com/argoproj/argo-helm/tree/main/charts/argo-cd) |

## Application Discovery Pattern

The hub's root Application (`projects/gke-cluster/`) syncs two hand-maintained trees:

```
projects/platform-gke/{component}/application.yaml → projects/platform/{component}/ (chart plus values-gke.yaml)
projects/gke-apps/{service}/application.yaml       → OCI chart plus projects/{service}/deploy/values*.yaml
```

The generated `projects/home-cluster/` auto-discovery root was retired with the home configuration in #6914.
