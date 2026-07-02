# Services Overview

This document provides an overview of all services running in the cluster.

## Core Infrastructure (cluster-critical)

| Service                      | Purpose                                                                        | Location                                                                                                   |
| ---------------------------- | ------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------- |
| **ArgoCD**                   | GitOps controller for declarative cluster management                           | [projects/platform/argocd](../projects/platform/argocd/)                                                   |
| **cert-manager**             | X.509 certificate management; required by Linkerd for mTLS                     | [projects/platform/cert-manager](../projects/platform/cert-manager/)                                       |
| **CoreDNS**                  | Cluster DNS resolution for Kubernetes services                                 | [projects/platform/coredns](../projects/platform/coredns/)                                                 |
| **Kyverno**                  | Policy engine with auto OTEL/Linkerd injection                                 | [projects/platform/kyverno](../projects/platform/kyverno/)                                                 |
| **Linkerd**                  | Service mesh providing default mTLS and metrics; optional tracing when enabled | [projects/platform/linkerd](../projects/platform/linkerd/)                                                 |
| **Longhorn**                 | Distributed persistent storage with automated backups                          | [projects/platform/longhorn](../projects/platform/longhorn/)                                               |
| **NVIDIA GPU Operator**      | GPU support for LLM inference workloads                                        | [projects/platform/nvidia-gpu-operator](../projects/platform/nvidia-gpu-operator/)                         |
| **OpenTelemetry Operator**   | Auto-instrumentation for Go, Python, Node.js                                   | [projects/platform/opentelemetry-operator](../projects/platform/opentelemetry-operator/)                   |
| **SigNoz**                   | Self-hosted observability (metrics, logs, traces)                              | [projects/platform/signoz](../projects/platform/signoz/)                                                   |
| **SigNoz Dashboard Sidecar** | GitOps sidecar for syncing SigNoz dashboards                                   | [projects/platform/signoz-addons/dashboard-sidecar](../projects/platform/signoz-addons/dashboard-sidecar/) |
| **1Password Operator**       | Secret management via OnePasswordItem CRDs                                     | External chart (Helm install, outside ArgoCD)                                                              |

## Production Services (prod)

| Service                | Purpose                                     | Location                                                                         |
| ---------------------- | ------------------------------------------- | -------------------------------------------------------------------------------- |
| **Cloudflare Gateway** | Zero Trust ingress (no open firewall ports) | [projects/platform/cloudflare-gateway](../projects/platform/cloudflare-gateway/) |
| **SeaweedFS**          | Distributed S3-compatible object storage    | [projects/platform/seaweedfs](../projects/platform/seaweedfs/)                   |

## Development Services (dev)

| Service             | Purpose                             | Location                                                                     |
| ------------------- | ----------------------------------- | ---------------------------------------------------------------------------- |
| **Grimoire**        | D&D knowledge management with Redis | [projects/grimoire](../projects/grimoire/)                                   |
| **OCI Model Cache** | HuggingFace model caching operator  | [projects/operators/oci-model-cache](../projects/operators/oci-model-cache/) |

## Public Web

The public website (apex `jomcgi.dev`, including `/docs`, the `/app/*` apps, and
the CV) is served by the monolith's read-only public tier, not standalone static
sites. See [monolith-public](../projects/monolith-public/) and the
[monolith frontend](../projects/monolith/frontend/). The old Astro/VitePress
Cloudflare Pages frontends were decommissioned (ADR docs/002).

## Service Details

For detailed information about specific services, see the README in each project directory:

- `projects/<service>/README.md`
- `projects/platform/<service>/README.md`
