# ADR 040: Caller-Provided Context Injection for Agent Guests (`/injected-context/`)

**Author:** jomcgi
**Status:** Draft
**Created:** 2026-07-03
**Builds on:** [041 - Hot Git Mirror for goosecracker Agent Workspaces](041-hot-git-mirror-agent-workspaces.md) (the guest-hydration model this adds a third input to), [030 - fc-invoke Configurable Firecracker Surface](030-fc-invoke-configurable-firecracker-surface.md) (the opaque payload the new field rides in), [025 - Three-Layer Agent Stack](025-three-layer-agent-stack-goosecracker.md) (places bundle assembly in the `goosecracker`/chat layer, not the substrate), 035 - Discord Multiplayer Agent UX (the thread-scoped per-turn loop the bundle is rebuilt inside)

---

## Problem

An agent guest today can see two things: the repo (cold-fetched from the hot mirror per ADR 041) and its own prior goose session (restored from the persisted `sessionDb` blob on resume). It cannot see any context that lives in the _caller_ and is neither in the repo nor in the agent's own conversation.

The concrete failure: a user asked the Discord agent "Does our recent conversation about Loom's future match the open roadmap items?" The agent happily read `docs/ROADMAP.md` and `docs/FUTURE.md` from the workspace, then answered "I don't have access to the prior conversation you're referencing." The repo hydrated; the conversation did not. The prior conversation lived in another Discord channel/thread, a surface the thread-scoped session (ADR 035) cannot see.

Two things are missing, and only one is the subject of this ADR:

1. A **mechanism** to hand an agent arbitrary caller-side context as files it can grep, without the substrate or guest learning what that context is or where it came from.
2. A **source** that fills that mechanism from Discord messages.

We want the mechanism to be source-agnostic (transport, not policy), the source to live behind the existing `chat.api` boundary, and the injected context to **evolve across turns** so the agent does not lose it on the second or third reply.

---

## Decision

Add a third opaque payload input alongside `task` and `sessionDb`: an **`injectedContext` bundle**, a map of filename to content, which the guest unpacks into `/injected-context/`. Structurally it is the sibling of the two inputs the guest already receives this way (`task` -> `/tmp/goose/task.md`, `sessionDb` -> the goose sessions DB).

The design keeps knowledge in exactly one layer:

| Layer                            | Knows the source? | Responsibility                                                                     |
| -------------------------------- | :---------------: | ---------------------------------------------------------------------------------- |
| fc-invoke daemon                 |        No         | Opaque reverse proxy; never parses the payload (unchanged).                        |
| Guest handler                    |        No         | Unpack the bundle to `/injected-context/`. Pure transport.                         |
| `goosecracker` `run_and_deliver` |        No         | Ask a provider for "context for this session," pack it, ship it, **every turn**.   |
| The producer (behind `chat.api`) |      **Yes**      | Turn Discord messages into files, tier-filter them, and describe their provenance. |

This mirrors ADR 041's split exactly: there the substrate "boots an opaque rootfs ... never learning there is a git mirror," and `goosecracker` owns provisioning. Here the substrate and guest handler stay context-agnostic, and the chat layer owns the bundle. The directory is named `/injected-context/`, not `/conversation-context/`, precisely so the guest cannot assume the payload is conversation or that it came from Discord.

Two properties fall out of rebuilding the bundle **inside the per-turn loop** (`run_and_deliver` calls `_run_one_turn` per turn; the bundle is built beside `sessions.load` at `runner.py:525`, which already round-trips per turn):

- **Persistence across turns.** The guest is ephemeral (`/workspace` and `/tmp` are tmpfs that die with each turn), so the bundle is re-injected on every turn rather than expected to survive. The agent cannot lose `/injected-context/` on turn 2 or 3.
- **Evolution.** Because the producer rebuilds from current state each turn, turn 3's bundle already contains turns 1 and 2. The injected context grows as the thread advances.

Provenance is carried **inside the bundle** (Option A below): the producer writes a self-describing `/injected-context/README.md` stating what each file is and where it came from. The recipe carries a single generic, source-blind line ("if `/injected-context/` exists, it holds caller-provided context for this task; read `README.md` first, then grep as needed"). The recipe names no source, so the substrate/recipe stay agnostic while the human-readable "this is Discord thread X" lives with the injector.

| Aspect                                       | Today                              | Decided                                            |
| -------------------------------------------- | ---------------------------------- | -------------------------------------------------- |
| Guest inputs                                 | repo (mirror), `task`, `sessionDb` | + `injectedContext` bundle -> `/injected-context/` |
| Caller-side context (other threads/channels) | invisible to the agent             | grep-able files, rebuilt per turn                  |
| Who knows the source                         | n/a                                | only the `chat.api` producer                       |
| Guest / substrate / fc-invoke                | repo- and goose-agnostic           | also context-agnostic (opaque bundle)              |
| Provenance description                       | n/a                                | self-describing `README.md` in the bundle          |
| Cross-turn behaviour                         | n/a                                | persists and accumulates (per-turn rebuild)        |
| Persistence surface                          | tmpfs (ephemeral)                  | tmpfs (ephemeral; no new durable store)            |

---

## Architecture

```mermaid
graph LR
    subgraph host["monolith (host)"]
        prov["chat.api.build_injected_context(session, tier)\nDiscord messages to files + README\n(tier-filtered)"]
        run["goosecracker run_and_deliver\n_run_one_turn (per turn)\nbeside sessions.load"]
        prov -->|opaque file map| run
    end
    run -->|payload.injectedContext\n(base64)| fc["fc-invoke daemon\n(opaque, unchanged)"]
    fc -->|HTTP over vsock| h["guest handler\nunpack bundle"]
    h -->|write files| dir["/injected-context/\n(tmpfs)"]
    dir -->|grep / read| goose["goose recipe\ngeneric 'consult /injected-context/' line"]
```

The only layer that references Discord is `chat.api.build_injected_context`. `goosecracker` receives an opaque `{filename: content}` map and never learns the word "Discord"; it reaches the provider across the same `chat.api` boundary it already uses for `ensure_steering_token` and `mark_inflight_running` (enforced by `import_boundaries_test`). A future second source (e.g. claude.ai history) is aggregated inside the provider; the guest, the substrate, and `run_and_deliver` do not change.

---

## Alternatives Considered

- **Stuff the context into the prompt.** Eager (pays tokens whether or not the agent needs it) and hard-capped by the context window; a curated summary fits but a raw transcript never will. Rejected: does not scale and wastes tokens the agent may not spend.
- **Carry the context in the git mirror (ADR 041 machinery).** Reuses the existing hydration path with zero new guest code, but writes private per-turn chat transcripts into permanent git history and adds per-turn commit churn to the hot path. Rejected in one sentence: transcripts do not belong in durable git history, and tmpfs injection avoids it entirely.
- **A chat-specific payload field (e.g. `discordMessages`).** Couples the substrate/guest to a single channel. Rejected: the interface must be source-agnostic so a second channel needs no substrate change.
- **Describe the context in the prompt/task text (Option B), not a bundled README.** Simplest, but re-pollutes every turn's user message with boilerplate and fails silently if a producer injects files but forgets the sentence (invisible files). Rejected in favour of the self-describing bundle plus a generic recipe line.
- **Live-mutate a warm guest (vsock file push / shared virtio-fs mount).** Only necessary if guests persisted across turns. Rejected: guests are ephemeral per turn, so regenerate-on-hydrate is strictly simpler and has no concurrency hazard.

---

## Security

Baseline in `docs/security.md`. The injected bundle is user data (chat messages) crossing into an isolated guest, so:

- **Tier-filtered at the source.** The producer packs only messages the session's trust tier is allowed to see (tier is already threaded through `submit()`/`env`; tier is the credential trust boundary). A lower-trust thread never receives a higher-trust conversation.
- **Ephemeral only.** The bundle lands in guest tmpfs (`/injected-context/`) and dies with the turn. It is never written to git, the warm base image, or any durable store, so there is no cross-session residue.
- **No egress widening.** Injected context is inbound files only; it does not touch the ADR 023 egress allowlist. The guest gains data, not new network reach.

---

## Risks

| Risk                                                   | Likelihood | Impact                    | Mitigation                                                                                                                  |
| ------------------------------------------------------ | ---------- | ------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| Bundle bloats the spin-up hot path                     | Medium     | Latency regression        | Cap message count / total bytes in the producer; `log()` truncation so a silent cap never reads as "full history injected". |
| Producer injects files but the recipe never greps them | Low        | Feature silently dead     | Generic source-blind recipe line guarantees discovery; README anchors the read.                                             |
| Cross-tier leakage of private conversation             | Low        | Privacy breach            | Tier filter in the producer + ephemeral tmpfs; unit-test the filter with the SQLite fixtures.                               |
| Self-describing README drifts from file contents       | Low        | Agent misreads provenance | Producer regenerates README in the same per-turn build as the files.                                                        |

---

## Open Questions

1. **Second source.** claude.ai conversation history is deferred; Discord-only for now. When added, it is aggregated inside the provider with no substrate change.
2. **Bundle encoding and cap.** base64 tar vs a JSON `{filename: content}` map, and the exact size/message-count ceiling, are implementation choices for the plan.
3. **Within-thread transcript.** Whether the thread's _own_ turns also become a grep-able file, or are left to goose session-state resume. Leaning external-only: session-state already owns within-thread continuity, so `/injected-context/` carries only what the session cannot otherwise see.

---

## References

| Resource                                                                         | Relevance                                                                                    |
| -------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| [ADR 041 - Hot Git Mirror](041-hot-git-mirror-agent-workspaces.md)               | The guest-hydration model this adds a third input to; the repo-agnostic-substrate precedent. |
| [ADR 030 - fc-invoke Surface](030-fc-invoke-configurable-firecracker-surface.md) | The opaque payload the new field rides in; daemon stays stateless.                           |
| [ADR 025 - Three-Layer Agent Stack](025-three-layer-agent-stack-goosecracker.md) | Places bundle assembly in the goosecracker/chat layer, not the substrate.                    |
| ADR 035 - Discord Multiplayer Agent UX    | The thread-scoped per-turn loop the bundle is rebuilt inside.                                |
| `projects/monolith/goosecracker/runner.py`                                       | `run_and_deliver` per-turn loop; `_run_one_turn` payload build beside `sessions.load`.       |
| `projects/firecracker/goosecracker/guest-init/internal/handler/handler.go`       | `AgentRequest` struct and the task-file/sessionDb unpack the new field mirrors.              |
