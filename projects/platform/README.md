# Platform

Cluster-critical infrastructure: the pieces every other service in this repo depends on
but none of them own. Everything here is deployed the same way as any other service
(an ArgoCD `Application` under `deploy/`, synced from Git), it just happens to be the
plumbing rather than a product.

This README is the component index. Current state and decision rationale live in
[ARCHITECTURE.md](ARCHITECTURE.md), including which cluster (the GKE hub or the
residual home cluster) each component runs on.

## GitOps

[ArgoCD](argocd/) is the control plane: it watches this repo and reconciles each cluster
to match. `projects/home-cluster/kustomization.yaml` is the generated app-of-apps root
for the home cluster; the hub's root is `projects/gke-cluster/`, which lists the
`projects/platform-gke/` overlays over the charts in this directory. There is no
`helm install` or `kubectl apply` in the normal workflow, only commits.

## Ingress and traffic

| Component                                    | Purpose                                                                                                                                                                                                                |
| -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cloudflare-gateway` | Envoy Gateway control plane, Gateway API resources, and the Cloudflare Tunnel. All external traffic enters here; nothing is exposed to the internet directly.                                                          |
| `cf-ingress-library` | Shared Helm library chart defining the trusted/public Cloudflare ingress tiers that individual services build their `HTTPRoute`s from.                                                                                 |
| `coredns`            | Cluster DNS configuration for the K3s nodes.                                                                                                                                                                           |

## Observability

| Component                    | Purpose                                                                                                  |
| ---------------------------- | -------------------------------------------------------------------------------------------------------- |
| `otel-collector`             | Deny-by-default trace collector and public URL probes exporting to Honeycomb.                            |
| `opentelemetry-operator`     | OpenTelemetry auto-instrumentation operator; production language instrumentation is disabled.           |

## Storage

| Component                            | Purpose                                                                                                                         |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------- |
| `longhorn`         | Distributed block storage backing Kubernetes `PersistentVolume`s.                                                               |
| `cloudnative-pg`   | Postgres operator; every service's database is a `Cluster` custom resource rather than a hand-run instance.                     |
| `atlas-operator`   | Schema migration operator: reads each service's migrations ConfigMap and applies them to its Postgres database.                 |

## Policy and scheduling

| Component                                      | Purpose                                                                                                                      |
| ---------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `kyverno`              | Policy engine for admission, resource audits, and cross-namespace Secret replication.                                       |
| `priority-classes`     | Cluster-scoped `PriorityClass`es underpinning the memory-oversubscription policy (see ARCHITECTURE.md, section 6).           |
| `keda`                 | Event-driven autoscaler; installed at home with no `ScaledObject` yet, so nothing scales on it.                              |
| `node-traffic-shaper`  | CAKE ingress qdisc for the home GPU node's uplink; inert under Cilium `tcx` (#4171), a decommission candidate.               |
| `nvidia-gpu-operator`  | GPU driver management and device-plugin configuration for the inference nodes.                                               |
| `argo-workflows`       | Namespace-scoped Argo Workflows engine for off-pod batch job execution, used by the monolith's job scheduler.                |
| `renovate`             | Daily self-hosted dependency updates, executed as an Argo CronWorkflow with credentials sourced from 1Password.              |
| `spire`                | SPIFFE workload identity control plane (ADR embervm/041); hub only, no consumer validates an SVID yet.                       |

Most components have their own README with configuration detail; where one doesn't
exist yet, the `application.yaml`/`Chart.yaml` in that directory is the source of truth.
