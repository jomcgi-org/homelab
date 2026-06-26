# Architecture Decision Records

ADRs document significant architectural decisions and their context.

## Agents

| ADR                                                                                                  | Decision                                                                                                                                          |
| ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| [001 - Background Agents](agents/001-background-agents.md)                                           | Kubernetes-native agent execution with sandbox isolation                                                                                          |
| [002 - OpenHands Agent Sandbox](agents/002-openhands-agent-sandbox.md)                               | OpenHands as the agent runtime framework                                                                                                          |
| [003 - Context Forge](agents/003-context-forge.md)                                                   | IBM Context Forge as the MCP gateway                                                                                                              |
| [004 - Autonomous Agents](agents/004-autonomous-agents.md)                                           | Design for fully autonomous agent workflows                                                                                                       |
| [005 - Role-Based MCP Access](agents/005-role-based-mcp-access.md)                                   | Role-based access control for MCP tool servers                                                                                                    |
| [006 - OIDC Auth MCP Gateway](agents/006-oidc-auth-mcp-gateway.md)                                   | OAuth 2.1 / OIDC authentication for remote MCP access                                                                                             |
| [007 - Agent Run Orchestration Service](agents/007-agent-orchestrator.md)                            | Dedicated service for dispatching and tracking agent job runs                                                                                     |
| [008 - Cluster Patrol Loop Resilience](agents/008-cluster-patrol-loop-resilience.md)                 | Crash recovery and per-sweep supervision for cluster_agents loops                                                                                 |
| [009 - Automated Test Generation Bots](agents/009-automated-test-generation.md)                      | Agent-driven test generation pipeline                                                                                                             |
| [010 - Recipe-Driven Agent Registry](agents/010-recipe-driven-agent-registry.md)                     | Goose recipe YAML as the source of truth for agent definitions                                                                                    |
| [011 - Agent MCP v1 Follow-ons](agents/011-agent-mcp-v1-followons.md)                                | Deferred self-improvement loop scope after v1 MCP surface shipped                                                                                 |
| [011 - Cloudflare Managed OAuth](agents/011-cloudflare-managed-oauth.md)                             | Cloudflare-managed OAuth for the MCP gateway (duplicate number)                                                                                   |
| [012 - Knowledge Gardener Model Pipeline](agents/012-knowledge-gardener-model-pipeline.md)           | Two-tier model pipeline for the knowledge gardener                                                                                                |
| [013 - Knowledge Gardener Gemma4-Only](agents/013-knowledge-gardener-gemma4-only.md)                 | Single-model pipeline replacement for the gardener                                                                                                |
| [014 - AX + Substrate Agent Runtime](agents/014-ax-substrate-agent-runtime.md)                       | Split-roles adoption of google/ax + agent-substrate, retiring orchestrator + cluster_agents                                                       |
| [015 - Temporal as Orchestration Substrate](agents/015-temporal-orchestration-substrate.md)          | Adopt Temporal for workflow execution + scheduling; supersedes ADR 014                                                                            |
| [016 - NATS as Canonical Event Stream](agents/016-nats-canonical-event-stream.md)                    | NATS JetStream as the system-wide event bus between independently-owned components                                                                |
| [017 - Domain Event Schema](agents/017-domain-event-schema.md)                                       | Event envelope schema + tombstone semantics across the system                                                                                     |
| [018 - Event-Driven Gardener Triggering](agents/018-event-driven-gardener-trigger.md)                | Monolith pushes gardening sessions via remote-trigger `run` on note edits; drops cron + queue                                                     |
| [019 - Substrate Executor + AgentWorkflow over Argo](agents/019-substrate-executor-agentworkflow.md) | Thin Substrate executor interface (agent-sandbox warm pool impl #1) under Argo; revisits 015's warm-pool dismissal for caller-blocked dispatch    |
| [020 - Deprecate Context Forge](agents/020-deprecate-context-forge-mcp-gateway.md)                   | Remove the MCP gateway; serve the monolith's MCP directly (auth stays at the Cloudflare edge). Supersedes 003. Validated plan, deferred execution |
| [021 - Discord-Triggered AgentWorkflow with Fast Hosted Model](agents/021-discord-triggered-agentworkflow-fast-model.md) | Discord bot (qwen gate) as a new AgentWorkflow consumer riding 019's submit path; fast hosted model (Gemini 3.5 Flash) over an OpenAI-compatible seam; snapshot/resume for smooth many-thread work. Draft |

## Docs

| ADR                                                                                                               | Decision                                                                                                       |
| ----------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| [001 - Static Docs Site](docs/001-static-docs-site.md)                                                            | VitePress for architecture documentation (superseded by 002)                                                   |
| [002 - Retire Standalone Web Frontends, Docs into Monolith](docs/002-websites-decommission-docs-into-monolith.md) | Delete `projects/websites/` + trips/hikes Pages frontends; serve docs from the monolith at `jomcgi.dev/docs/*` |

## Networking

| ADR                                                                          | Decision                                      |
| ---------------------------------------------------------------------------- | --------------------------------------------- |
| [001 - Cloudflare Envoy Gateway](networking/001-cloudflare-envoy-gateway.md) | Cloudflare Tunnel + Envoy Gateway for ingress |

## Platform

| ADR                                                                                                                  | Decision                                                                                          |
| -------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| [001 - Obsidian Vault Monolith Migration](platform/001-obsidian-vault-monolith-migration.md)                         | Migrate Obsidian vault into the monolith on TigerFS                                               |
| [002 - CDN-Cached Data Fetching](platform/002-cdn-cached-data-fetching.md)                                           | Public JSON endpoints cache at the Cloudflare edge; clients poll cached                           |
| [003 - CDN Cache Rule Scoped to `public.jomcgi.dev`](platform/003-cdn-cache-hostname-rule.md)                        | Scope CDN cache rule to public.jomcgi.dev (supersedes 002 partially)                              |
| [004 - Iceberg-on-SeaweedFS Lakehouse with Hot-Swap Quack Serving](platform/004-iceberg-lakehouse-hot-swap.md)       | Event-sourced lakehouse; NATS → Iceberg → Quack hot-swap; partially evolves 001                   |
| [005 - Per-PR Preview Environments](platform/005-per-pr-preview-environments.md)                                     | Ephemeral monolith previews: CoW Postgres clone, muted side effects, ApplicationSet PR generator  |
| [006 - Decommission Obsidian via a Postgres Interim](platform/006-obsidian-decommission-postgres-interim.md)         | Kill Obsidian now: note body authoritative in Postgres, web UI editor; interim ahead of 004       |
| [007 - SeaweedFS Bucket Provisioning via COSI](platform/007-seaweedfs-bucket-provisioning-cosi.md)                   | Declarative buckets + lifecycle + per-app creds via COSI; replaces create-only weed-shell Jobs    |
| [008 - Monolith Module Boundaries](platform/008-monolith-module-boundaries.md)                                       | Internal module boundaries within the monolith                                                    |
| [009 - Post-Merge Chart Versioning and Kargo Promotion](platform/009-post-merge-chart-versioning-kargo-promotion.md) | Bump versions post-merge on main, not on branches; Kargo dev->prod promotion with synthetic gates |

## Security

| ADR                                                                                            | Decision                                                                                                                                   |
| ---------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| [001 - Bazel Semgrep](security/001-bazel-semgrep.md)                                           | Semgrep SAST integrated via Bazel rules                                                                                                    |
| [002 - Semgrep Rule Generation via RL](security/002-semgrep-rule-generation-rl.md)             | RL-finetuned Qwen 3.5 9B for generating Semgrep rules from CVEs                                                                            |
| [003 - gVisor RuntimeClass](security/003-gvisor-runtime-class.md)                              | User-space kernel isolation for agent sandbox pods via runsc                                                                               |
| [004 - Public Read-Only Service Isolation](security/004-public-read-only-service-isolation.md) | Separate read-only public service on a replica, isolated from private data and secrets                                                     |
| [005 - Public Chat Adversarial Hardening](security/005-public-chat-adversarial-hardening.md)   | Defense-in-depth for anonymous GPU-backed chat: Turnstile sessions, reserved-headroom semaphore, server-side limits, DB-confined retrieval |

## Services

| ADR                                                                                    | Decision                                                                      |
| -------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| [001 - Discord History Backfill](services/001-discord-history-backfill.md)             | One-time backfill of Discord channel history into pgvector                    |
| [002 - Discord Chat Automation](services/002-discord-chat-automation.md)               | Scheduling, triggers, and proactive posting for the Discord bot               |
| [010 - FastMonolith Modular Framework](services/010-fastmonolith-modular-framework.md) | Privilege-typed, data-isolated domain modules composed into per-tier binaries |

## Tooling

| ADR                                                                                             | Decision                                                                                                                         |
| ----------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| [001 - OCI Tool Distribution](tooling/001-oci-tool-distribution.md)                             | Multi-arch OCI image for developer tools, eliminating local Bazel                                                                |
| [002 - Service Deployment Tooling](tooling/002-service-deployment-tooling.md)                   | Copier template to scaffold new services, eliminating per-service boilerplate                                                    |
| [003 - Spec-First CLI and Skills](tooling/003-spec-first-cli-and-skills.md)                     | OpenAPI as source of truth; CLI commands and Claude skills are derived                                                           |
| [004 - OCaml Rules for Semgrep](tooling/004-ocaml-rules-for-semgrep.md)                         | Scale the custom bazel/ocaml ruleset (not obazl); ppx first, per-arch native toolchains                                          |
| [005 - tOyCaml Demonstrator](tooling/005-toycaml-demonstrator.md)                               | Engine-shaped demonstrator exercising the ruleset before Semgrep lands                                                           |
| [006 - Multi-arch OCaml Toolchains](tooling/006-extensible-multiarch-ocaml-toolchains.md)       | Data-driven arch registry; per-arch toolchain registration gated on pool verification                                            |
| [007 - OCaml BUILD Generation](tooling/007-ocaml-build-file-generation-gazelle.md)              | Gazelle-based BUILD file generation for OCaml sources                                                                            |
| [008 - CLI Multi-platform Distribution](tooling/008-cli-multiplatform-distribution.md)          | One Bazel graph, native execution platforms (cloud arm64, self-hosted darwin); no cross-compilation, QEMU, or wasm               |
| [009 - Bazel-native Package Classification](tooling/009-bazel-native-package-classification.md) | Tag/visibility per-package over central globs and gazelle:exclude; lint the old pattern out                                      |
| [010 - Hermetic Visual Regression](tooling/010-hermetic-visual-regression.md)                   | Move public-page screenshot capture/diff into cached Bazel actions on an apko chromium image; non-frontend PRs become cache hits |
