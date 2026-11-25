# Archived projects

These projects are no longer on `main`. Their full source stays at a git tag.
Read a project at its tag, not on `main`. The PR that deletes a project adds
its row here.

`hub-final` is the last `main` commit with the GKE hub code (HOME-262). The
hub restore drill uses it.

| Project | What it was | Removed in | Source | Replaced by |
|---|---|---|---|---|
| embervm | Firecracker agent substrate for the hub | [#8720](https://github.com/jomcgi-org/homelab/pull/8720) | [hub-final](https://github.com/jomcgi-org/homelab/tree/hub-final/projects/embervm) | ate (`projects/platform-factory/substrate`) |
| firecracker | Guest images and utilities for Firecracker VMs | [#8720](https://github.com/jomcgi-org/homelab/pull/8720) | [hub-final](https://github.com/jomcgi-org/homelab/tree/hub-final/projects/firecracker) | ate |
| model-bench | CLI that ran agent task fixtures and ranked models | [#8720](https://github.com/jomcgi-org/homelab/pull/8720) | [hub-final](https://github.com/jomcgi-org/homelab/tree/hub-final/projects/model-bench) | Production trial arms (HOME-244) |
| operators | `oci-model-cache` Kubernetes operator | [#8720](https://github.com/jomcgi-org/homelab/pull/8720) | [hub-final](https://github.com/jomcgi-org/homelab/tree/hub-final/projects/operators) | None |
| sextant | Generator for type-safe Go operator state machines | [#8720](https://github.com/jomcgi-org/homelab/pull/8720) | [hub-final](https://github.com/jomcgi-org/homelab/tree/hub-final/projects/sextant) | None |
| mcp | Hub MCP gateway and servers | [#8720](https://github.com/jomcgi-org/homelab/pull/8720) | [hub-final](https://github.com/jomcgi-org/homelab/tree/hub-final/projects/mcp) | None |
| loom | Cluster wiring for loom and its Postgres | [#8720](https://github.com/jomcgi-org/homelab/pull/8720) | [hub-final](https://github.com/jomcgi-org/homelab/tree/hub-final/projects/loom) | None |
| inference | Hub CPU embeddings service (llama.cpp) | [#8720](https://github.com/jomcgi-org/homelab/pull/8720) | [hub-final](https://github.com/jomcgi-org/homelab/tree/hub-final/projects/inference) | Voyage API (Hindsight) |
| oom-inference | Inference engine for MoE models larger than GPU and RAM | [#8720](https://github.com/jomcgi-org/homelab/pull/8720) | [hub-final](https://github.com/jomcgi-org/homelab/tree/hub-final/projects/oom-inference) | None |
| monolith-agents | Authenticated MCP and knowledge tier of the monolith | [#8720](https://github.com/jomcgi-org/homelab/pull/8720) | [hub-final](https://github.com/jomcgi-org/homelab/tree/hub-final/projects/monolith-agents) | None |
| gke-cluster | Hub Argo CD root applications | [#8720](https://github.com/jomcgi-org/homelab/pull/8720) | [hub-final](https://github.com/jomcgi-org/homelab/tree/hub-final/projects/gke-cluster) | `projects/factory-cluster` |
| gke-apps | Hub application manifests | [#8720](https://github.com/jomcgi-org/homelab/pull/8720) | [hub-final](https://github.com/jomcgi-org/homelab/tree/hub-final/projects/gke-apps) | `projects/factory-cluster` |
| platform-gke | Hub platform services (Tailscale, Argo Workflows, Argo CD) | [#8720](https://github.com/jomcgi-org/homelab/pull/8720) | [hub-final](https://github.com/jomcgi-org/homelab/tree/hub-final/projects/platform-gke) | `projects/platform-factory` |
| platform/kyverno | Kyverno and its GHCR secret policies | [#8720](https://github.com/jomcgi-org/homelab/pull/8720) | [hub-final](https://github.com/jomcgi-org/homelab/tree/hub-final/projects/platform/kyverno) | None |
| platform/argo-workflows | Argo Workflows for monolith batch jobs | [#8720](https://github.com/jomcgi-org/homelab/pull/8720) | [hub-final](https://github.com/jomcgi-org/homelab/tree/hub-final/projects/platform/argo-workflows) | None |
| platform/atlas-operator | Atlas schema operator values | [#8720](https://github.com/jomcgi-org/homelab/pull/8720) | [hub-final](https://github.com/jomcgi-org/homelab/tree/hub-final/projects/platform/atlas-operator) | None |
| platform/polylane | Polylane read-only Kubernetes agent | [#8720](https://github.com/jomcgi-org/homelab/pull/8720) | [hub-final](https://github.com/jomcgi-org/homelab/tree/hub-final/projects/platform/polylane) | None |
| platform/honeycomb | Honeycomb alert triggers for the hub | [#8720](https://github.com/jomcgi-org/homelab/pull/8720) | [hub-final](https://github.com/jomcgi-org/homelab/tree/hub-final/projects/platform/honeycomb) | Prometheus alerts |
| platform/priority-classes | PriorityClasses for memory oversubscription | [#8720](https://github.com/jomcgi-org/homelab/pull/8720) | [hub-final](https://github.com/jomcgi-org/homelab/tree/hub-final/projects/platform/priority-classes) | `projects/platform-factory/priority` |
| platform/spire | SPIRE workload identity for EmberVM | [#8720](https://github.com/jomcgi-org/homelab/pull/8720) | [hub-final](https://github.com/jomcgi-org/homelab/tree/hub-final/projects/platform/spire) | None yet (HOME-254) |
| ships | AIS vessel tracker | [#8719](https://github.com/jomcgi-org/homelab/pull/8719) | [hub-final](https://github.com/jomcgi-org/homelab/tree/hub-final/projects/ships) | None |
