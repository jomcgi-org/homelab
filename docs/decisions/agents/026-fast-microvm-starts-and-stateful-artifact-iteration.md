# ADR 026: Fast MicroVM Cold Starts and Stateful Artifact Iteration

**Author:** jomcgi
**Status:** Draft
**Created:** 2026-06-29
**Builds on:** [022 - Firecracker Snapshot/Restore Controller](022-firecracker-snapshot-restore-controller.md) (the `fc-agentd` substrate, the `RootfsProvisioner` interface, the reconcile loop), [024 - Discord Agent, Hosted-Model Tiers, and Isolated Live Artifacts](024-discord-agent-hosted-model-tiers-and-artifacts.md) (the goosecracker artifact tier and the `ARTIFACT_ID = Discord thread` identity), [023 - Egress Secret Proxy](023-egress-secret-proxy.md) (the per-tier credential model the artifact tier rides)

---

## Problem

goosecracker (024) works end to end: a Discord `/goosecracker` boots a Firecracker microVM, runs goose against a hosted model (Gemini 3.5 Flash via OpenRouter, key swapped at egress), publishes a sandboxed artifact at `jomcgi.dev/artifact/<id>`, and streams build progress back to the thread. Running it live surfaced two latency and cost problems.

**1. Every run is a true cold start, and it is slower than it needs to be.** Measured from real runs: a cold start is roughly 5 to 7 seconds, made of three parts:

- **0 to 5s reconcile claim.** `fc-agentd` polls desired vs actual state every 5 seconds (`ReconcileInterval`); a `PENDING` thread waits for the next tick.
- **~2s rootfs provision.** The default `CopyProvisioner` does a full ~3GB copy of the base rootfs to a per-thread file on every run.
- **~0.5s boot + handshake.** Firecracker boot, vsock, and the `fc-agent-init` Hello/Assign.

There is no warm pool and no reuse: every run pays all three.

**2. Every Discord-thread iteration re-runs the entire build from scratch.** A goosecracker thread is a conversation ("make a bouncing ball" then "make the balls bigger" then "add gravity"), but each reply re-runs goose from scratch over the full accumulated transcript (Model B). So each iteration:

- pays the full cold start again, plus
- pays the **full ~80 to 110s build** again (almost entirely model reasoning), because the model regenerates the whole artifact from a paragraph describing the whole spec rather than editing what already exists, and
- gets **zero inference prefix-cache benefit**: each run sends a fresh, small prompt (recipe plus the owner instructions), so the long, stable conversation prefix that an inference prefix cache would reward never exists. The egress capture confirms it: `prompt_tokens_details.cached_tokens` is `0`.

Two tempting non-answers were ruled out:

- **A warm pool does not scale.** It pays idle RAM for N pre-booted VMs, absorbs burst only up to the pool size, and past that is cold again. It hides the cost at a fixed capacity rather than removing it.
- **VM snapshot-resume does not give the model-latency win it appears to.** The model KV/prefix cache lives on the inference side (OpenRouter/Gemini, or in-cluster vLLM), not in the guest microVM. Snapshotting the VM preserves nothing about that cache. What earns a prefix-cache hit is re-sending the exact conversation prefix within the provider's cache window, which is a property of the conversation, not of the VM image.

## Decision

Two decisions, sequenced: the first is the scalable floor and the fallback path for the second.

### 1. Fast cold starts: copy-on-write rootfs plus event-driven dispatch

Make a cold start fast enough (target under 1 second) that it stops mattering, with zero idle cost at any concurrency.

- **Copy-on-write rootfs provisioner.** Replace `CopyProvisioner`'s full 3GB copy with a CoW provisioner behind the existing `RootfsProvisioner` interface (022 already documents this as "a devmapper thin-COW impl is a future efficiency follow-up behind the same interface"). Use devmapper thin-snapshots (already proven on node-4 for devmapper image-seeding) or a filesystem reflink on the rootfs volume. Per-thread rootfs creation drops from ~2s to milliseconds, and per-thread disk drops from a full copy to only the written delta.
- **Event-driven dispatch.** Remove the 0 to 5s claim wait by waking the reconcile loop on a `PENDING` insert (Postgres `LISTEN`/`NOTIFY`, or a lightweight signal from `dispatch.submit`), keeping the 5s poll as a safety net rather than the primary path.

This benefits every run (first build and iteration), needs no standing capacity, and is the cold fallback for decision 2.

### 2. Stateful artifact iteration (Model A) via persisted goose session and artifact, not VM snapshots

Treat a Discord thread as a session and make iterations incremental.

The mechanism is **goose's own file-based session resume**, validated in a spike against goose 1.27.1 (the pinned harness version):

- goose stores conversations in a SQLite database at `~/.local/share/goose/sessions/sessions.db`. `goose run --name <id> --resume -t "<instruction>"` resumes the named session: it replays the full prior conversation to the model and continues. This is file-based, not process-based, so no live VM or snapshot is required.
- The spike proved cross-VM portability: after a two-turn session, copying out `sessions.db`, wiping the entire sessions directory (simulating a fresh VM), restoring only that file, and resuming still recalled the earlier turn's content. The DB is self-contained and portable.

So:

- **First message in a thread: cold build (Model B).** No session exists, so cold-boot a fast VM and run the full artifact task as today, under a session named for the Discord thread.
- **Each reply: restore and resume (Model A).** Restore the thread's persisted `sessions.db` and the prior artifact into a fresh fast-cold VM, then `goose run --name <thread> --resume -t "<new instruction>"`. The model re-sends the exact conversation prefix, so it (a) hits the inference prefix cache (cheaper input, faster TTFT) and (b) edits the artifact in place, generating only the delta instead of regenerating the whole file.
- **Persist the session and artifact per thread, keyed by `ARTIFACT_ID` (the Discord thread).** The artifact already lives in `s3://artifacts/<id>/index.html` (024). Persist `sessions.db` (kilobytes; goose exits between turns so the file is consistent at export time) alongside it.
- **Always fall back to cold plus Model B** when the session is missing, unreadable, or fails to resume (for example after a goose upgrade changes the SQLite schema).

The model-latency and cost win therefore comes from preserving the **conversation**, which decision 1 (fast cold starts) makes cheap to rehydrate. A VM snapshot would only additionally save the sub-second boot, which is not worth its version-binding fragility once cold starts are fast.

## Consequences

**Positive.**

- Cold start drops from ~5 to 7s to under 1s for every run, scaling to arbitrary concurrency with no idle cost.
- Iterations become incremental: the per-reply build shrinks from the full ~80 to 110s regeneration to a small edit, and starts earning inference prefix-cache hits (`cached_tokens > 0`) instead of zero.
- Iteration semantics improve independently of caching: goose edits the existing file rather than rebuilding from a transcript, which is more faithful to "change this one thing."
- No standing GPU or RAM cost (no warm pool), and no large per-thread snapshot storage.

**Costs and risks.**

- New persistence surface: per-thread `sessions.db` storage plus an export/restore path, and a TTL/eviction policy so abandoned sessions do not accumulate.
- A goose-CLI dependency, now **validated** (spike, goose 1.27.1): `goose run --name <id> --resume -t "<instruction>"` resumes a named SQLite-backed session and replays full history, and the `sessions.db` is portable across a wiped/fresh environment. One small open detail for implementation: whether to re-pass `--recipe` on resume or rely on the recipe's system prompt already being in the session (turn 1 wrote it). Low risk; the cold-plus-Model-B fallback contains it.
- Session staleness across goose upgrades: a `sessions.db` written by one goose version may not open under another if the SQLite schema changes. The fallback handles correctness; the cost is an occasional iteration that silently cold-rebuilds.
- Provider implicit-cache TTL is short (minutes), and Discord iterations can be minutes apart, so a prefix-cache hit is not guaranteed even with a perfect prefix. The cache-independent win (incremental output instead of full regeneration) does not depend on this; explicit Gemini context caching is a later knob if iteration cadence proves slow.

## Alternatives considered

- **Warm VM pool.** Rejected as the primary mechanism: idle cost, bounded burst absorption, and it does not remove the cold-start cost, it pre-pays it at a fixed capacity. Fast cold starts dominate it on scalability.
- **VM snapshot-resume per thread.** Rejected as the iteration mechanism. The anatomy matters: a Firecracker snapshot is a small **state file** (VM/device config) plus a **memory file** sized by the guest RAM the run touched. Copy-on-write (decision 1) shrinks the **rootfs**, which is a separate backing file, not the memfile, so "CoW makes the snapshot small" conflates the two; only a _sparse_ memfile plus _diff_ snapshots shrink the memory part, and it stays proportional to touched RAM. But size was never the main objection:
  - **It does not buy the cache win.** The model KV/prefix cache is inference-side. Both snapshot-resume and session-file resume re-send the same conversation prefix, so `cached_tokens` is identical either way. A snapshot buys **boot time**, not cache hits.
  - **It is version- and node-bound.** A memory snapshot is tied to the exact kernel, Firecracker version, CPU feature set, and device layout. Every harness or kernel deploy invalidates every thread's snapshot, and we deploy the harness frequently, so snapshots would be invalidated faster than reused and you would cold-start (and need the session-file fallback) anyway.
    Once decision 1 makes a cold boot sub-second, a snapshot saves only a few hundred milliseconds over "fresh VM plus restore session file," at the cost of that version-binding fragility. It remains a possible boot-latency micro-optimization layered on decision 1 once the harness deploy cadence settles, never the iteration mechanism.
- **Explicit Gemini context caching.** A complementary later option to defeat the short implicit-cache TTL on slow iteration cadences; out of scope here because the incremental-output win is larger and cache-provider-independent.
