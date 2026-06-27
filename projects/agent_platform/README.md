# agent_platform

Agent execution substrate for the homelab. Implements the Firecracker
snapshot/restore controller from
[ADR 022](../../docs/decisions/agents/022-firecracker-snapshot-restore-controller.md),
the `Snapshotable` executor behind the `Substrate` seam of
[ADR 019](../../docs/decisions/agents/019-substrate-executor-agentworkflow.md), and
the engine behind the idle-thread smoothness of
[ADR 021](../../docs/decisions/agents/021-discord-triggered-agentworkflow-fast-model.md).

Plan: [docs/plans/2026-06-27-firecracker-snapshot-restore-controller.md](../../docs/plans/2026-06-27-firecracker-snapshot-restore-controller.md).

## Layout

```
agent_platform/
├── substrate/      # The thin Substrate interface (ADR 019): core Claim/Exec/Release
│                   # plus optional Suspendable/Snapshotable/Persistent capabilities,
│                   # with an in-memory fake for testing consumers with no cluster.
└── fc-agentd/      # The controller daemon (ADR 022): a node-4 Postgres-reconcile loop
    ├── cmd/        #   entrypoint
    └── internal/
        ├── config/     # env-driven configuration
        ├── store/      # Postgres-backed claude_agent.agent_threads registry
        ├── reconcile/  # the desired-vs-actual control loop
        └── telemetry/  # OTLP/SigNoz tracing
```

## Components (per the plan)

- **Controller (`fc-agentd`)**: a node-4 daemon. Postgres-reconcile loop: read
  desired thread state, drive Firecracker (boot/pause/snapshot/restore), write
  actual state back. Owns storage + GC, restore routing, node/arch affinity, the
  Substrate seam.
- **Wrapper (`fc-agent-init`)**: the microVM's PID 1 (Phase 2). Owns idle/quiescence
  detection, the snapshot signal to the controller over vsock, and reconnect on resume.
- **Registry**: `claude_agent.agent_threads` in the monolith Postgres, plus the
  monolith MCP catalog tools and a UI page (Phase 3).
- **Backstop**: a scheduled routine over the registry (timeout sweep + warm-base
  refresh) (Phase 4).

## The AgentThread

The durable unit is the **AgentThread**, keyed by a stable `thread_id` assigned at
create and never changed across snapshot/restore. The id is the contract; node,
snapshot file, Postgres task, and Discord thread are lookups off it. Snapshots are
never load-bearing: durable state stays in Postgres, so a lost snapshot degrades
(re-init) rather than losing work.

```
PENDING --restore(base)--> RUNNING --idle--> pause+snapshot --> IDLE
                             ^                                    |
                             +--------restore(thread)<---wake-----+
RUNNING --task done--> COMPLETED --> reclaim (delete snapshot + volume)
```

## Dispatch and catalog (the monolith side)

The control plane is Postgres (`claude_agent.agent_threads` +
`claude_agent.agent_base_snapshots`), so consumers never talk to Firecracker
directly: they read and write the registry over the monolith MCP surface, and
`fc-agentd` reconciles it.

- **Dispatch** (`projects/monolith/agent/dispatch.py`, ADR 019/021 seam):
  `submit(task, thread_id?)` creates a new `PENDING` thread (resolving the
  repo's built warm base when one exists) or resumes an existing `IDLE` thread;
  `status(thread_id)` reads the row. Wake triggers: `wake(thread_id)` (manual /
  CI event for a known id) and `wake_for_discord_thread(discord_thread)` (a
  reply arrived). Exposed as `monolith-agent-submit-agent-task` and
  `monolith-agent-wake-agent-thread-for-discord`.
- **Catalog**: `monolith-agent-list-agent-threads`, `-get-agent-thread`,
  `-resume-agent-thread`, `-list-agent-bases`, `-request-base-rebuild`,
  `-run-agent-backstop`.

The ADR 021 Discord consumer (qwen gate) and the CI webhook call `submit` / the
wake triggers; that bolt-in and a read-only catalog UI page are the remaining
integration follow-ups. End-to-end validation of snapshot/restore continuity
runs on node-4 (where `/dev/kvm` exists); CI covers the controller logic, the
registry, and the catalog with unit and real-Postgres BDD tests.
