# Platform

Cluster-critical infrastructure: the pieces every other service in this repo depends on
but none of them own. Everything here is deployed the same way as any other service
(an ArgoCD `Application` under `deploy/`, synced from Git), it just happens to be the
plumbing rather than a product.

This README is the component index. Current state and decision rationale live in
[ARCHITECTURE.md](ARCHITECTURE.md). The GKE hub is retired (HOME-262). The
factory cluster deploys the shared charts here through `projects/platform-factory/`.
Tag `hub-final` keeps the hub overlays and the hub-only charts deleted with it.

## GitOps

[ArgoCD](argocd/) is the control plane: it watches this repo and reconciles the
factory cluster to match. Its root is `projects/factory-cluster/`, which lists the
`projects/platform-factory/` Applications over the charts in this directory. There is no
`helm install` or `kubectl apply` in the normal workflow, only commits.

## Ingress and traffic

| Component                                    | Purpose                                                                                                                                                                                                                |
| -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cloudflare-gateway` | Envoy Gateway control plane, Gateway API resources, and the Cloudflare Tunnel. All external traffic enters here; nothing is exposed to the internet directly.                                                          |
| `cf-ingress-library` | Shared Helm library chart defining the trusted/public Cloudflare ingress tiers that individual services build their `HTTPRoute`s from.                                                                                 |

## Observability

| Component                    | Purpose                                                                                                  |
| ---------------------------- | -------------------------------------------------------------------------------------------------------- |
| `otel-collector`             | Deny-by-default trace collector and public URL probes exporting to Honeycomb.                            |

## Storage

| Component                            | Purpose                                                                                                                         |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------- |
| `cloudnative-pg`   | Postgres operator; every service's database is a `Cluster` custom resource rather than a hand-run instance.                     |

## Policy and scheduling

| Component                                      | Purpose                                                                                                                      |
| ---------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `renovate`             | Factory daily dependency updates and weekly apko lock maintenance. Both hub schedules are suspended pending factory verification and hub retirement (HOME-88). |
| `onepassword`          | 1Password Connect and the 1Password Operator, the secret broker every `OnePasswordItem` is reconciled through; hub only.     |
| `authentik` | Identity provider at `auth.jomcgi.dev`; OAuth2 providers and applications are blueprints. |
| `cert-manager` | Certificate issuance and renewal, including operator webhook CAs. |
| `kargo` | Promotes published chart versions to the Kargo-owned Applications. |

Most components have their own README with configuration detail; where one doesn't
exist yet, the `Chart.yaml` in that directory and its `projects/platform-factory/`
Application are the source of truth.

The `stpa` directory holds the source for the [CI and supply chain security model](STPA.md).
