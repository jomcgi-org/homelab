# Agent Platform

Autonomous AI agents in isolated Kubernetes sandbox pods.

## Overview

Claude and Goose agents dispatched by a Go orchestrator over NATS JetStream, with tool access governed by Context Forge (homelab MCP gateway) and RBAC-scoped per team. External access authenticated via Cloudflare Managed OAuth.

See [docs/agents.md](../../docs/agents.md) for the full architecture.

| Component                | Description |
| ------------------------ | ----------- |
| **orchestrator**         | Go service that dispatches agent jobs via NATS JetStream. Also contains an MCP server (`mcp/`), a React web UI (`ui/`), and a CLI runner (`cmd/runner/`) |
| **sandboxes**            | Shell setup script for MCP profiles (`setup-mcp-profiles.sh`); Kubernetes sandbox pod definitions live in `chart/sandboxes/` and `chart/agent-sandbox/` |
| **cluster_agents**       | Go service that monitors cluster health and runs five autonomous improvement agents: `patrol` (escalates firing SigNoz alerts), `TestCoverageAgent`, `ReadmeFreshnessAgent`, `RulesAgent`, and `PRFixAgent`. Shared utilities: `AlertCollector` polls SigNoz for firing alerts (used by patrol); `GitActivityGate` gates improvement-agent runs on recent git activity; `Escalator` deduplicates findings and dispatches jobs to the orchestrator; `Runner` supervises all agent loops with per-sweep timeouts and panic recovery, restarting any agent that exits unexpectedly; `NotificationPublisher` publishes notifications to Discord channels via NATS; `GitHubClient` calls the GitHub API to find open PRs with failing CI checks (used by `PRFixAgent`). |
| **api_gateway**          | Nginx-based API gateway with route-based backend selection for api.jomcgi.dev. Has its own Helm chart and ArgoCD Application in `api_gateway/deploy/` (not part of the umbrella chart), with nginx reverse proxy, collector sidecar (periodically writes cluster status JSON), and SLO alerting templates. |
| **goose_agent**          | Goose agent container and configuration. Includes 19 agent recipe YAML files in `image/recipes/` (deep-plan, code-fix, research, web-research, adr-writer, bazel, ci-debug, feature, pr-review, and more) and the apko container image definition in `image/`. |
| **vllm**                 | Alternative LLM serving backend (full Helm chart with templates and vendored dependencies in `vllm/deploy/` — not wired to ArgoCD) |
| **chart**                | Umbrella Helm chart bundling orchestrator, goose-sandboxes, MCP servers, NATS, and agent-sandbox (`cluster_agents` and `api_gateway` have separate ArgoCD Applications) |
| **deploy**               | ArgoCD Application, kustomization, and cluster-specific values |
