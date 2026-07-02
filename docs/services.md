# Services Overview

This document provides an overview of all services running in the cluster.

## Core Infrastructure (cluster-critical)

| Service                      | Purpose                                                                        | Location                                                                                                   |
| ---------------------------- | ------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------- |
| **Agent Sandbox**            | Controller for isolated agent execution pods                                   | [projects/platform/agent-sandbox](../projects/platform/agent-sandbox/)                                     |
| **ArgoCD**                   | GitOps controller for declarative cluster management                           | [projects/platform/argocd](../projects/platform/argocd/)                                                   |
| **ArgoCD Image Updater**     | Automatic image updates for ArgoCD-managed applications                        | [projects/platform/argocd-image-updater](../projects/platform/argocd-image-updater/)                       |
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

| Service                | Purpose                                                             | Location                                                                                   |
| ---------------------- | ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| **API Gateway**        | External service routing with rate limiting                         | [projects/agent_platform/api_gateway](../projects/agent_platform/api_gateway/)             |
| **Cloudflare Gateway** | Zero Trust ingress (no open firewall ports)                         | [projects/platform/cloudflare-gateway](../projects/platform/cloudflare-gateway/)           |
| **Context Forge**      | MCP gateway for aggregating tool servers                            | [projects/agent_platform/context_forge](../projects/agent_platform/context_forge/)         |
| **Goose Sandboxes**    | Goose agent sandbox deployments                                     | [projects/agent_platform/sandboxes](../projects/agent_platform/sandboxes/)                 |
| **Knowledge Graph**    | RSS scraping, embedding, and MCP search                             | [projects/blog_knowledge_graph](../projects/blog_knowledge_graph/)                         |
| **llama-cpp**          | Local LLM inference                                                 | [projects/agent_platform/llama_cpp](../projects/agent_platform/llama_cpp/)                 |
| **MCP OAuth Proxy**    | OAuth 2.1 auth layer for remote MCP access                          | [projects/agent_platform/mcp_oauth_proxy](../projects/agent_platform/mcp_oauth_proxy/)     |
| **MCP Servers**        | Consolidated ArgoCD, Kubernetes, BuildBuddy, and SigNoz MCP servers | [projects/agent_platform/mcp_servers_chart](../projects/agent_platform/mcp_servers_chart/) |
| **NATS**               | High-performance messaging with JetStream                           | [projects/platform/nats](../projects/platform/nats/)                                       |
| **SeaweedFS**          | Distributed S3-compatible object storage                            | [projects/platform/seaweedfs](../projects/platform/seaweedfs/)                             |
| **Todo**               | Git-backed todo list with static UI                                 | [projects/todo_app](../projects/todo_app/)                                                 |
| **Trips**              | Trip management service                                             | [projects/trips](../projects/trips/)                                                       |

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
