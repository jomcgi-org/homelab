# Architecture Decision Records

ADRs document significant architectural decisions and their context.

## Agents

| ADR                                                                                        | Decision                                                                                    |
| ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------- |
| [001 - Background Agents](agents/001-background-agents.md)                                 | Kubernetes-native agent execution with sandbox isolation                                    |
| [002 - OpenHands Agent Sandbox](agents/002-openhands-agent-sandbox.md)                     | OpenHands as the agent runtime framework                                                    |
| [003 - Context Forge](agents/003-context-forge.md)                                         | IBM Context Forge as the MCP gateway                                                        |
| [004 - Autonomous Agents](agents/004-autonomous-agents.md)                                 | Design for fully autonomous agent workflows                                                 |
| [005 - Role-Based MCP Access](agents/005-role-based-mcp-access.md)                         | Role-based access control for MCP tool servers                                              |
| [006 - OIDC Auth MCP Gateway](agents/006-oidc-auth-mcp-gateway.md)                         | OAuth 2.1 / OIDC authentication for remote MCP access                                       |
| [007 - Agent Run Orchestration Service](agents/007-agent-orchestrator.md)                  | Dedicated service for dispatching and tracking agent job runs                               |
| [008 - Cluster Patrol Loop Resilience](agents/008-cluster-patrol-loop-resilience.md)       | Crash recovery and per-sweep supervision for cluster_agents loops                           |
| [009 - Automated Test Generation Bots](agents/009-automated-test-generation.md)            | Agent-driven test generation pipeline                                                       |
| [010 - Recipe-Driven Agent Registry](agents/010-recipe-driven-agent-registry.md)           | Goose recipe YAML as the source of truth for agent definitions                              |
| [011 - Agent MCP v1 Follow-ons](agents/011-agent-mcp-v1-followons.md)                      | Deferred self-improvement loop scope after v1 MCP surface shipped                           |
| [011 - Cloudflare Managed OAuth](agents/011-cloudflare-managed-oauth.md)                   | Cloudflare-managed OAuth for the MCP gateway (duplicate number)                             |
| [012 - Knowledge Gardener Model Pipeline](agents/012-knowledge-gardener-model-pipeline.md) | Two-tier model pipeline for the knowledge gardener                                          |
| [013 - Knowledge Gardener Gemma4-Only](agents/013-knowledge-gardener-gemma4-only.md)       | Single-model pipeline replacement for the gardener                                          |
| [014 - AX + Substrate Agent Runtime](agents/014-ax-substrate-agent-runtime.md)             | Split-roles adoption of google/ax + agent-substrate, retiring orchestrator + cluster_agents |

## Docs

| ADR                                                    | Decision                                 |
| ------------------------------------------------------ | ---------------------------------------- |
| [001 - Static Docs Site](docs/001-static-docs-site.md) | VitePress for architecture documentation |

## Networking

| ADR                                                                          | Decision                                      |
| ---------------------------------------------------------------------------- | --------------------------------------------- |
| [001 - Cloudflare Envoy Gateway](networking/001-cloudflare-envoy-gateway.md) | Cloudflare Tunnel + Envoy Gateway for ingress |

## Platform

| ADR                                                                                          | Decision                                                                |
| -------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| [001 - Obsidian Vault Monolith Migration](platform/001-obsidian-vault-monolith-migration.md) | Migrate Obsidian vault into the monolith on TigerFS                     |
| [002 - CDN-Cached Data Fetching](platform/002-cdn-cached-data-fetching.md)                   | Public JSON endpoints cache at the Cloudflare edge; clients poll cached |

## Security

| ADR                                                                                | Decision                                                        |
| ---------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| [001 - Bazel Semgrep](security/001-bazel-semgrep.md)                               | Semgrep SAST integrated via Bazel rules                         |
| [002 - Semgrep Rule Generation via RL](security/002-semgrep-rule-generation-rl.md) | RL-finetuned Qwen 3.5 9B for generating Semgrep rules from CVEs |
| [003 - gVisor RuntimeClass](security/003-gvisor-runtime-class.md)                  | User-space kernel isolation for agent sandbox pods via runsc    |

## Services

| ADR                                                                        | Decision                                                        |
| -------------------------------------------------------------------------- | --------------------------------------------------------------- |
| [001 - Discord History Backfill](services/001-discord-history-backfill.md) | One-time backfill of Discord channel history into pgvector      |
| [002 - Discord Chat Automation](services/002-discord-chat-automation.md)   | Scheduling, triggers, and proactive posting for the Discord bot |

## Tooling

| ADR                                                                           | Decision                                                                      |
| ----------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| [001 - OCI Tool Distribution](tooling/001-oci-tool-distribution.md)           | Multi-arch OCI image for developer tools, eliminating local Bazel             |
| [002 - Service Deployment Tooling](tooling/002-service-deployment-tooling.md) | Copier template to scaffold new services, eliminating per-service boilerplate |
| [003 - Spec-First CLI and Skills](tooling/003-spec-first-cli-and-skills.md)   | OpenAPI as source of truth; CLI commands and Claude skills are derived        |
