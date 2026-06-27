# Plan: Firecracker Snapshot/Restore Controller for AgentWorkflow

Implements [ADR 022](../decisions/agents/022-firecracker-snapshot-restore-controller.md)
(extending [019](../decisions/agents/019-substrate-executor-agentworkflow.md) /
[021](../decisions/agents/021-discord-triggered-agentworkflow-fast-model.md)). Builds the
controller that gives an idle agent thread ~0 cost and sub-second wake, resuming exactly
where it paused.

## Decisions carried in (all settled)

| Question                 | Decision                                                                                                      |
| ------------------------ | ------------------------------------------------------------------------------------------------------------- |
| Substrate                | **FC-direct** (drive Firecracker; the kata-fc devmapper substrate on node-4 is reused)                        |
| Snapshot bundle          | **Full snapshots first**; diff snapshots are a fast follow (Phase 6)                                          |
| Registry / control plane | **Postgres** table in the monolith; controller is a Postgres-reconcile loop, not a CRD operator               |
| Idle detection           | In-VM **wrapper** (no-activity AND quiescent) + a **Goose-routine** timeout backstop                          |
| Base warming             | **Two repo-specific warm bases** (repo@main baked + repo env), refreshed every 15-30 min when `main` advances |
| Isolation                | Firecracker microVM (gVisor rejected)                                                                         |
| Scale                    | Deferred (homelab-fine; revisit before open-sourcing)                                                         |

## Grounding (already derisked on node-4)

Raw FC spike: boot 273 ms, snapshot create 822 ms (16 KB state + 1 GB mem), restore
**28 ms cold / 6 ms warm**, continuity proven (heartbeat resumed, did not reboot). The
File backend mmaps the mem image and faults pages lazily, so restore is sub-second without
UFFD yet. Reference architecture to port: `e2b-dev/infra` (Apache-2.0).

## Components

- **Controller** (`fc-agentd`): a node-4 daemon. Postgres-reconcile loop: read desired
  thread state, drive Firecracker (boot/pause/snapshot/restore), write actual state back.
  Owns storage + GC, restore routing, node/arch affinity, the Substrate seam.
- **Wrapper** (`fc-agent-init`): the microVM's PID 1, launches the agent harness. Owns
  idle/quiescence detection, snapshot-signal (vsock to controller), reconnect-on-resume.
- **Registry**: Postgres table(s) in the monolith; MCP tools + a UI page for the catalog.
- **Backstop**: a scheduled Goose routine over the registry (timeout sweep + warm-base refresh).

## Phases

Each phase is independently shippable and verifiable. CI is the test loop (push, watch).

### Phase 0 - Foundations

- Define the Go `Substrate` interface (`Claim`/`Exec`/`Release` + `Snapshotable.Snapshot`/`Restore`) per ADR 019, with an in-memory fake for tests.
- Postgres schema: `agent_threads` (thread_id PK, state, repo, branch, node, arch, base_snapshot_ref, thread_snapshot_ref, size_bytes, created_at, last_active_at, ttl, discord_thread) as an Atlas migration.
- `fc-agentd` skeleton: config, Postgres connection, a no-op reconcile loop, SigNoz instrumentation.
- **Done when**: the interface + fake pass tests in CI; the migration applies; the daemon starts and idles cleanly.

### Phase 1 - FC-direct snapshot/restore (the core)

- Productionize the derisk: a Go FC driver that boots a microVM (kata kernel + a thread rootfs on devmapper), and does pause -> `CreateSnapshot` -> `LoadSnapshot`+resume over the FC API socket.
- Snapshot bundle (full): snapfile + memfile + rootfs, dir-per-thread on `/disks/nvme-02`, keyed by thread_id. Port E2B's bundle layout.
- Wire it as the `Snapshotable` impl behind the Substrate interface.
- **Done when**: `fc-agentd` can, given a thread_id, boot -> snapshot -> restore-resume a microVM and the restored guest continues (the heartbeat test, automated).

### Phase 2 - In-VM wrapper

- `fc-agent-init`: launches the harness, monitors activity, detects idle = no CPU activity AND no in-flight model/MCP call (quiescent), signals the controller over vsock with the wake condition.
- Reconnect-on-resume: on restore, re-establish model/MCP/git clients before handing back to the harness.
- **Done when**: a thread auto-snapshots at a quiescent idle boundary and, after restore, the harness continues with live connections (no mid-call snapshot).

### Phase 3 - Postgres registry + control loop + catalog

- Implement the reconcile loop: desired vs actual; create/restore/snapshot/reclaim transitions; LISTEN/NOTIFY (or poll) for wake events.
- Catalog: monolith MCP tools (`list-agent-threads`, `get-agent-thread`, `resume-agent-thread`) + a `/app/...` UI page reading the table.
- GC: TTL/idle eviction of snapshots; pool-headroom guard on `/disks/nvme-02`.
- **Done when**: threads are visible/resumable from the catalog; idle threads are GC'd per TTL; the loop survives a daemon restart (state in Postgres).

### Phase 4 - Per-repo warm bases + backstop

- Build the two repo-specific bases (boot from the repo's env image, checkout `main`, warm the harness, snapshot -> `base_snapshot_ref`).
- A scheduled refresh (Goose routine / scheduled job, every 15-30 min): if a repo's `main` advanced, rebuild its base; in-flight idle threads (own snapshots) are untouched.
- The timeout-backstop sweep (same routine): park/alert long-idle/stuck threads the wrapper missed.
- **Done when**: a new thread restores its repo's warm base (repo at near-fresh-main, no full clone); bases auto-refresh on main-change only.

### Phase 5 - AgentWorkflow dispatch integration

- Substrate-keyed interface: `submit(task, threadId?) -> threadId` (new = create+restore-base+run; with id = resume), `status(threadId)`.
- Wire to ADR 021's Discord consumer (qwen gate -> submit) and wake triggers: CI webhook, Discord reply, manual.
- **Done when**: a Discord-triggered task runs in a snapshot-managed microVM end to end, goes idle/snapshots, and wakes-and-continues on a reply or CI event.

### Phase 6+ (fast follows, post-iteration-1)

- **Diff snapshots**: base + per-thread dirty-page diffs (port E2B) to cut per-idle-thread disk from ~guest-RAM to the delta.
- **UFFD**: if/when full-mmap restore latency degrades under load.
- **Scale characterization**: diff sizes, GC budget, restore p50/p99 under contention (revisit before open-sourcing).

## Risks / notes

- FC-direct means `fc-agentd` owns FC process supervision (crash cleanup, orphan reaping) - mirror E2B's patterns.
- Single node (node-4) for now; snapshots are node/arch-bound, so restore routing is trivial until a second AMD node exists.
- The wrapper<->controller vsock channel is the one new in-guest/host contract; keep it minimal (idle-signal, wake-condition, resume-ack).
- Snapshots are never load-bearing: durable task state stays in monolith Postgres, so a lost snapshot degrades (re-init) rather than loses work.

## Execution

Per the repo convention, execute subagent-driven once this plan is approved, one comprehensive review at the end of each merged PR.
