# Platform

Cluster-critical infrastructure: the pieces every other service in this repo depends on
but none of them own. Everything here is deployed the same way as any other service
(an ArgoCD `Application` under `deploy/`, synced from Git), it just happens to be the
plumbing rather than a product.

## GitOps

[ArgoCD](argocd/) is the control plane: it watches this repo and reconciles the cluster
to match. `projects/home-cluster/kustomization.yaml` is the generated app-of-apps root
that ArgoCD itself watches; every `projects/*/deploy/application.yaml` is discovered
through it. There is no `helm install` or `kubectl apply` in the normal workflow, only
commits.

## Ingress and traffic

| Component                                    | Purpose                                                                                                                                                                                                                |
| -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [`cloudflare-gateway/`](cloudflare-gateway/) | Envoy Gateway control plane, Gateway API resources, and the Cloudflare Tunnel. All external traffic enters here; nothing is exposed to the internet directly.                                                          |
| [`cf-ingress-library/`](cf-ingress-library/) | Shared Helm library chart defining the trusted/public Cloudflare ingress tiers that individual services build their `HTTPRoute`s from.                                                                                 |
| [`coredns/`](coredns/)                       | Cluster DNS configuration for the K3s nodes.                                                                                                                                                                           |

## Observability

| Component                                                  | Purpose                                                                                                                          |
| ---------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| [`signoz/`](signoz/)                                       | Unified metrics, logs, and traces platform, the main dashboard for the cluster.                                                  |
| [`signoz-addons/`](signoz-addons/)                         | Extra collectors and integrations layered on top of the base SigNoz deployment (for example the vLLM/Prometheus metrics bridge). |
| [`signoz-dashboards-library/`](signoz-dashboards-library/) | Shared Helm library chart of reusable SigNoz dashboard definitions that services import rather than hand-rolling JSON.           |
| [`opentelemetry-operator/`](opentelemetry-operator/)       | OpenTelemetry Operator with auto-instrumentation for Python, Node.js, and Go workloads.                                          |

## Storage

| Component                            | Purpose                                                                                                                         |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------- |
| [`longhorn/`](longhorn/)             | Distributed block storage backing Kubernetes `PersistentVolume`s.                                                               |
| [`seaweedfs/`](seaweedfs/)           | S3-compatible distributed object storage, shared infrastructure for services that need blob storage rather than a block device. |
| [`cloudnative-pg/`](cloudnative-pg/) | Postgres operator; every service's database is a `Cluster` custom resource rather than a hand-run instance.                     |
| [`atlas-operator/`](atlas-operator/) | Schema migration operator: reads each service's migrations ConfigMap and applies them to its Postgres database.                 |

## Policy and scheduling

| Component                                      | Purpose                                                                                                                      |
| ---------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| [`kyverno/`](kyverno/)                         | Policy engine with `ClusterPolicies` that automate observability and service-mesh injection across namespaces.               |
| [`priority-classes/`](priority-classes/)       | Cluster-scoped `PriorityClass`es underpinning the memory-oversubscription policy (see ADR platform/010).                     |
| [`keda/`](keda/)                               | Event-driven autoscaler, used where plain CPU/memory-based HPA isn't the right signal.                                       |
| [`node-traffic-shaper/`](node-traffic-shaper/) | Caps inbound bandwidth on a node's uplink with CAKE so a bulk download can't starve latency-sensitive control-plane traffic. |
| [`nvidia-gpu-operator/`](nvidia-gpu-operator/) | GPU driver management and device-plugin configuration for the inference nodes.                                               |
| [`argo-workflows/`](argo-workflows/)           | Namespace-scoped Argo Workflows engine for off-pod batch job execution, used by the monolith's job scheduler.                |
| [`renovate/`](renovate/)                       | Daily self-hosted dependency updates, executed as an Argo CronWorkflow with credentials sourced from 1Password.              |

Most components have their own README with configuration detail; where one doesn't
exist yet, the `application.yaml`/`Chart.yaml` in that directory is the source of truth.
