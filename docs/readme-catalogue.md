# README catalogue

Inventory date: 2026-08-14

This catalogue covers the 61 tracked files whose basename starts with `README`.
The ignored README under `.codex/plugins/cache/` is intentionally excluded.

Staleness is based on the most recent Git commit touching each file:

- **Fresh**: changed within the last 90 days (2026-05-16 or later).
- **Aging**: changed 91 to 180 days ago (2026-03-18 through 2026-05-15).
- **Stale**: unchanged for more than 180 days.

This is a triage signal, not a claim that the content is correct. A recent
README can still describe a broken command, while a stable reference README
may not need frequent edits. Files that look generated, pinned, or vendor-like
are marked in the type column so they can be reviewed differently.

## Summary

| Type | Count | Fresh | Aging | Stale |
| --- | ---: | ---: | ---: | ---: |
| Repository, project, or component overview | 16 | 13 | 3 | 0 |
| Build and developer tooling | 15 | 12 | 3 | 0 |
| EmberVM runtime, deployment, and specification | 10 | 10 | 0 | 0 |
| Platform and operations | 17 | 12 | 5 | 0 |
| Reference, generated, or snapshot documentation | 3 | 3 | 0 | 0 |
| **Total** | **61** | **50** | **11** | **0** |

## Catalogue

### Repository, project, or component overview

| README | Role | Last changed | Status |
| --- | --- | --- | --- |
| [README.md](../README.md) | Repository entry point | 2026-08-07 | Fresh |
| [projects/firecracker/README.md](../projects/firecracker/README.md) | Firecracker project overview | 2026-08-07 | Fresh |
| [projects/firecracker/semgrep/README.md](../projects/firecracker/semgrep/README.md) | Semgrep guest component | 2026-08-07 | Fresh |
| [projects/firecracker/substrate/README.md](../projects/firecracker/substrate/README.md) | Shared Firecracker utilities | 2026-08-07 | Fresh |
| [projects/mcp/README.md](../projects/mcp/README.md) | MCP Gateway | 2026-08-10 | Fresh |
| [projects/model-bench/README.md](../projects/model-bench/README.md) | Model benchmark project | 2026-08-11 | Fresh |
| [projects/monolith/README.md](../projects/monolith/README.md) | Monolith project overview | 2026-08-09 | Fresh |
| [projects/monolith/chat/README.md](../projects/monolith/chat/README.md) | Discord bot | 2026-07-01 | Fresh |
| [projects/monolith/claude_routines/README.md](../projects/monolith/claude_routines/README.md) | Claude.ai routines source of truth | 2026-07-24 | Fresh |
| [projects/monolith/knowledge/README.md](../projects/monolith/knowledge/README.md) | Knowledge pipeline | 2026-07-01 | Fresh |
| [projects/operators/README.md](../projects/operators/README.md) | Operators project overview | 2026-07-03 | Fresh |
| [projects/operators/oci-model-cache/README.md](../projects/operators/oci-model-cache/README.md) | OCI model cache operator | 2026-07-03 | Fresh |
| [projects/platform/README.md](../projects/platform/README.md) | Platform overview | 2026-08-07 | Fresh |
| [projects/sextant/README.md](../projects/sextant/README.md) | Sextant project | 2026-06-09 | Fresh |
| [projects/shared/README.md](../projects/shared/README.md) | Shared project utilities | 2026-08-07 | Fresh |
| [tools/knowledge_research/README.md](../tools/knowledge_research/README.md) | Knowledge research tool | 2026-04-25 | Aging, review |

### Build and developer tooling

| README | Role | Last changed | Status |
| --- | --- | --- | --- |
| [bazel/helm/README.md](../bazel/helm/README.md) | Helm Bazel rules | 2026-07-01 | Fresh |
| [bazel/images/README.md](../bazel/images/README.md) | Image build tooling | 2026-08-10 | Fresh |
| [bazel/ocaml/README.md](../bazel/ocaml/README.md) | OCaml Bazel rules | 2026-07-23 | Fresh |
| [bazel/ocaml/examples/toycaml/README.md](../bazel/ocaml/examples/toycaml/README.md) | OCaml example | 2026-06-10 | Fresh |
| [bazel/requirements/README.md](../bazel/requirements/README.md) | Python requirements management | 2026-03-10 | Aging, review |
| [bazel/semgrep/README.md](../bazel/semgrep/README.md) | Semgrep Bazel rules | 2026-07-26 | Fresh |
| [bazel/semgrep/defs/README.md](../bazel/semgrep/defs/README.md) | Semgrep rule definitions | 2026-03-10 | Aging, review |
| [bazel/tools/README.md](../bazel/tools/README.md) | Bazel tools overview | 2026-07-24 | Fresh |
| [bazel/tools/buildbuddy/snapshots/README.md](../bazel/tools/buildbuddy/snapshots/README.md) | BuildBuddy usage snapshots | 2026-07-27 | Fresh |
| [bazel/tools/hf2oci/README.md](../bazel/tools/hf2oci/README.md) | Hugging Face to OCI tool | 2026-03-08 | Aging, review |
| [bazel/tools/image/README.md](../bazel/tools/image/README.md) | Image helper tooling | 2026-08-10 | Fresh |
| [bazel/tools/js/README.md](../bazel/tools/js/README.md) | JavaScript tooling | 2026-07-01 | Fresh |
| [bazel/tools/oci/README.md](../bazel/tools/oci/README.md) | OCI Bazel wrappers | 2026-08-10 | Fresh |
| [buck2/README.md](../buck2/README.md) | Buck2 build rules | 2026-06-14 | Fresh |
| [tools/claude-code-patch/README.md](../tools/claude-code-patch/README.md) | Claude Code resume patch | 2026-08-04 | Fresh |

### EmberVM runtime, deployment, and specification

| README | Role | Last changed | Status |
| --- | --- | --- | --- |
| [projects/embervm/README.md](../projects/embervm/README.md) | EmberVM overview | 2026-08-11 | Fresh |
| [projects/embervm/deploy/README.md](../projects/embervm/deploy/README.md) | Reference deployment | 2026-08-11 | Fresh |
| [projects/embervm/dev/deploy/README.md](../projects/embervm/dev/deploy/README.md) | Development deployment | 2026-08-13 | Fresh |
| [projects/embervm/runtimes/bazel/README.md](../projects/embervm/runtimes/bazel/README.md) | Bazel-query demo runtime | 2026-07-27 | Fresh |
| [projects/embervm/runtimes/claude/README.md](../projects/embervm/runtimes/claude/README.md) | Claude runtime | 2026-07-27 | Fresh |
| [projects/embervm/runtimes/k3s/README.md](../projects/embervm/runtimes/k3s/README.md) | K3s guest images | 2026-07-26 | Fresh |
| [projects/embervm/runtimes/k3s/drill/README.md](../projects/embervm/runtimes/k3s/drill/README.md) | Single-VM K3s drill | 2026-07-17 | Fresh |
| [projects/embervm/runtimes/python/README.md](../projects/embervm/runtimes/python/README.md) | Python runtime base | 2026-07-14 | Fresh |
| [projects/embervm/runtimes/python/testdata/echo/README.md](../projects/embervm/runtimes/python/testdata/echo/README.md) | Zip-lane smoke test | 2026-08-11 | Fresh |
| [projects/embervm/specs/README.md](../projects/embervm/specs/README.md) | TLA+ specifications | 2026-08-11 | Fresh |

### Platform and operations

| README | Role | Last changed | Status |
| --- | --- | --- | --- |
| [projects/platform/argocd/README.md](../projects/platform/argocd/README.md) | Argo CD | 2026-03-23 | Aging, review |
| [projects/platform/authentik/README.md](../projects/platform/authentik/README.md) | Authentik | 2026-08-08 | Fresh |
| [projects/platform/cert-manager/README.md](../projects/platform/cert-manager/README.md) | cert-manager | 2026-07-13 | Fresh |
| [projects/platform/cilium/bootstrap/README.md](../projects/platform/cilium/bootstrap/README.md) | Cilium bootstrap | 2026-07-23 | Fresh |
| [projects/platform/coredns/README.md](../projects/platform/coredns/README.md) | CoreDNS configuration chart | 2026-03-20 | Aging, review |
| [projects/platform/kargo/README.md](../projects/platform/kargo/README.md) | Kargo | 2026-08-14 | Fresh |
| [projects/platform/kyverno/README.md](../projects/platform/kyverno/README.md) | Kyverno | 2026-07-14 | Fresh |
| [projects/platform/longhorn/README.md](../projects/platform/longhorn/README.md) | Longhorn | 2026-07-03 | Fresh |
| [projects/platform/node-traffic-shaper/README.md](../projects/platform/node-traffic-shaper/README.md) | Node traffic shaper | 2026-06-14 | Fresh |
| [projects/platform/nvidia-gpu-operator/README.md](../projects/platform/nvidia-gpu-operator/README.md) | NVIDIA GPU Operator | 2026-03-10 | Aging, review |
| [projects/platform/renovate/README.md](../projects/platform/renovate/README.md) | Renovate | 2026-08-08 | Fresh |
| [projects/platform/seaweedfs/README.md](../projects/platform/seaweedfs/README.md) | SeaweedFS | 2026-07-03 | Fresh |
| [projects/platform/signoz-addons/dashboard-sidecar/README.md](../projects/platform/signoz-addons/dashboard-sidecar/README.md) | SigNoz dashboard sidecar | 2026-03-20 | Aging, review |
| [projects/platform/signoz-addons/operator/README.md](../projects/platform/signoz-addons/operator/README.md) | SigNoz operator | 2026-03-20 | Aging, review |
| [projects/platform/signoz/README.md](../projects/platform/signoz/README.md) | SigNoz | 2026-03-10 | Aging, review |
| [projects/platform/signoz/bootstrap/README.md](../projects/platform/signoz/bootstrap/README.md) | SigNoz bootstrap | 2026-07-26 | Fresh |
| [projects/platform/signoz-addons/operator/crds/README.md](../projects/platform/signoz-addons/operator/crds/README.md) | SigNoz operator CRDs | 2026-03-10 | Aging, review |

### Reference, generated, or snapshot documentation

| README | Role | Last changed | Status |
| --- | --- | --- | --- |
| [docs/decisions/embervm/README.md](../docs/decisions/embervm/README.md) | ADR index and reading guide | 2026-08-11 | Fresh |
| [docs/runbooks/README.md](../docs/runbooks/README.md) | Runbook index | 2026-08-10 | Fresh |
| [bazel/ocaml/semgrep_src/README.md](../bazel/ocaml/semgrep_src/README.md) | Pinned Semgrep CE tree note | 2026-06-16 | Fresh |

## Review queue

The 11 aging files are the first manual review queue. Start with operational
and dependency-sensitive documentation, then confirm whether the README still
matches its BUILD, chart, or deployment inputs:

1. [projects/platform/coredns/README.md](../projects/platform/coredns/README.md), [projects/platform/argocd/README.md](../projects/platform/argocd/README.md), and [projects/platform/nvidia-gpu-operator/README.md](../projects/platform/nvidia-gpu-operator/README.md)
2. [projects/platform/signoz/README.md](../projects/platform/signoz/README.md) and its [operator](../projects/platform/signoz-addons/operator/README.md), [dashboard sidecar](../projects/platform/signoz-addons/dashboard-sidecar/README.md), and [CRD](../projects/platform/signoz-addons/operator/crds/README.md) docs
3. [bazel/requirements/README.md](../bazel/requirements/README.md), [bazel/semgrep/defs/README.md](../bazel/semgrep/defs/README.md), and [bazel/tools/hf2oci/README.md](../bazel/tools/hf2oci/README.md)
4. [tools/knowledge_research/README.md](../tools/knowledge_research/README.md)

No tracked README meets the stale threshold. The next useful maintenance pass
is therefore a correctness check of the aging queue, not a broad rewrite.
