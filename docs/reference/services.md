# Services Overview

This document provides an overview of all services running in the cluster.

## Core Infrastructure (cluster-critical)

| Service                      | Purpose                                                                        | Location                                                                                                   |
| ---------------------------- | ------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------- |
| **ArgoCD**                   | GitOps controller for declarative cluster management                           | [projects/platform/argocd](../../projects/platform/argocd/)                                                   |
| **cert-manager**             | X.509 certificate management for in-cluster TLS                     | [projects/platform/cert-manager](../../projects/platform/cert-manager/)                                       |
| **CoreDNS**                  | Cluster DNS resolution for Kubernetes services                                 | [projects/platform/coredns](../../projects/platform/coredns/)                                                 |
| **Kyverno**                  | Policy engine with auto OTEL injection                                 | [projects/platform/kyverno](../../projects/platform/kyverno/)                                                 |
| **Cilium**                   | eBPF CNI: WireGuard pod-to-pod encryption, network policy, Hubble metrics      | [projects/platform/cilium](../../projects/platform/cilium/)                                                   |
| **Longhorn**                 | Distributed persistent storage with automated backups                          | [projects/platform/longhorn](../../projects/platform/longhorn/)                                               |
| **NVIDIA GPU Operator**      | GPU support for LLM inference workloads                                        | [projects/platform/nvidia-gpu-operator](../../projects/platform/nvidia-gpu-operator/)                         |
| **OpenTelemetry Operator**   | Auto-instrumentation for Go, Python, Node.js                                   | [projects/platform/opentelemetry-operator](../../projects/platform/opentelemetry-operator/)                   |
| **SigNoz**                   | Self-hosted observability (metrics, logs, traces)                              | [projects/platform/signoz](../../projects/platform/signoz/)                                                   |
| **SigNoz Dashboard Sidecar** | GitOps sidecar for syncing SigNoz dashboards                                   | [projects/platform/signoz-addons/dashboard-sidecar](../../projects/platform/signoz-addons/dashboard-sidecar/) |
| **Argo Workflows**           | Namespace-scoped batch-job executor for monolith workflows                     | [projects/platform/argo-workflows](../../projects/platform/argo-workflows/)                                   |
| **Atlas Operator**           | Declarative database schema migrations via Atlas CRDs                          | [projects/platform/atlas-operator](../../projects/platform/atlas-operator/)                                   |
| **CloudNativePG**            | PostgreSQL operator for in-cluster databases                                   | [projects/platform/cloudnative-pg](../../projects/platform/cloudnative-pg/)                                   |
| **KEDA**                     | Event-driven autoscaler, shared infrastructure                                 | [projects/platform/keda](../../projects/platform/keda/)                                                       |
| **Node Traffic Shaper**      | Caps inbound node bandwidth with CAKE to protect control-plane traffic         | [projects/platform/node-traffic-shaper](../../projects/platform/node-traffic-shaper/)                         |
| **1Password Operator**       | Secret management via OnePasswordItem CRDs                                     | External chart (Helm install, outside ArgoCD)                                                              |

## Production Services (prod)

| Service                | Purpose                                     | Location                                                                         |
| ---------------------- | ------------------------------------------- | -------------------------------------------------------------------------------- |
| **Cloudflare Gateway** | Zero Trust ingress (no open firewall ports) | [projects/platform/cloudflare-gateway](../../projects/platform/cloudflare-gateway/) |
| **SeaweedFS**          | Distributed S3-compatible object storage    | [projects/platform/seaweedfs](../../projects/platform/seaweedfs/)                   |
| **SeaweedFS node-4**   | Second SeaweedFS volume server on node-4 for replication | [projects/platform/seaweedfs-node4](../../projects/platform/seaweedfs-node4/) |
| **Monolith**           | Primary application backend and frontend    | [projects/monolith](../../projects/monolith/)                                       |
| **Monolith Public**    | Read-only public tier serving jomcgi.dev    | [projects/monolith-public](../../projects/monolith-public/)                         |
| **EmberVM**            | Firecracker microVM orchestration for agent sandboxes | [projects/embervm](../../projects/embervm/)                               |
| **Git Mirror**         | Hot in-cluster git mirror for Firecracker agent guests | [projects/firecracker/git-mirror](../../projects/firecracker/git-mirror/) |
| **Inference**          | Self-hosted LLM inference (vLLM)            | [projects/inference](../../projects/inference/)                                     |
| **Context Forge Gateway** | MCP gateway for the GitHub and monolith tool surfaces | [projects/mcp/context-forge-gateway](../../projects/mcp/context-forge-gateway/) |

## Development Services (dev)

| Service             | Purpose                             | Location                                                                     |
| ------------------- | ----------------------------------- | ---------------------------------------------------------------------------- |
| **Grimoire**        | D&D knowledge management with Redis | [projects/grimoire](../../projects/grimoire/)                                   |
| **OCI Model Cache** | HuggingFace model caching operator  | [projects/operators/oci-model-cache](../../projects/operators/oci-model-cache/) |

## Public Web

The public website (apex `jomcgi.dev`, including `/docs`, the `/app/*` apps, and
the CV) is served by the monolith's read-only public tier, not standalone static
sites. See [monolith-public](../../projects/monolith-public/) and the
[monolith frontend](../../projects/monolith/frontend/). The old Astro/VitePress
Cloudflare Pages frontends were decommissioned (ADR docs/002).

## Service Details

For detailed information about specific services, see the README in each project directory:

- `projects/<service>/README.md`
- `projects/platform/<service>/README.md`
