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
