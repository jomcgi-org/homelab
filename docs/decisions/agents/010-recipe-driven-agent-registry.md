# ADR 010: Recipe-Driven Agent Registry

**Author:** jomcgi
**Status:** Deprecated (superseded by claude.ai routines)
**Created:** 2026-03-12
**Extends:** [007-agent-orchestrator](007-agent-orchestrator.md)

---

## Problem

Agent recipes are baked into the goose-agent container image at build time. Adding or modifying a recipe requires rebuilding the image and redeploying the warm pool. The orchestrator and pipeline composer UI need a separate configuration to know which agents exist and how to display them, creating two sources of truth.

---

## Decision

Recipes become data managed by the orchestrator via Helm values. The orchestrator stores recipes in a ConfigMap, serves UI metadata to the frontend, and sends full recipe content to runners at dispatch time over HTTP.

---

## Consequences

- **Adding an agent** = adding an entry to `agentsConfig` in Helm values, redeploying orchestrator only
- **Goose-agent image** becomes a generic runtime — no baked-in recipes
- **Single source of truth** — Helm values define both UI metadata and recipe content
- **External extensibility** — umbrella chart consumers configure agents via values overrides
- **Runner simplification** — runner no longer discovers profiles from filesystem

---

## Future Architecture

This ADR establishes the foundation for further decoupling of the agent platform. The following are envisioned as future work:

### Runner as Sidecar

Move the runner HTTP API server from inside the goose-agent image to a separate sidecar container. The sidecar writes recipes to a shared `emptyDir` volume and manages goose lifecycle via the Kubernetes exec API. This separates concerns: the main container is pure goose runtime, the sidecar handles orchestration protocol.

### Vanilla Goose Image

With recipes sent over HTTP and the runner extracted to a sidecar, the main container becomes a vanilla goose installation with no custom tooling baked in. This simplifies image maintenance and makes it easier to track upstream goose releases.

### OCI Tools Volume Mount

CLI tools (gh, bb, format, etc.) currently baked into the goose-agent image could be packaged as a separate OCI image and mounted as an init container that copies tools to a shared volume. This decouples tool versions from the goose runtime.

### Dynamic Recipe Management

A future API for CRUD operations on recipes would allow runtime creation and modification of agents without Helm redeployments, enabling use cases like user-defined agents or A/B testing recipe variations.
