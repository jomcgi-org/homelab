-- Adds claude_agent.agent_threads: the registry and control plane for the
-- Firecracker snapshot/restore controller (ADR 022, decision 5). The durable
-- unit is the AgentThread, keyed by a stable thread_id assigned at create and
-- never changed across snapshot/restore. node, snapshot refs, the Postgres task,
-- and the Discord thread are all lookups off that id.
--
-- This table is the control plane: fc-agentd (a node-4 daemon) runs a
-- Postgres-reconcile loop reading desired thread state and writing actual state
-- back, and the monolith MCP catalog (list/get/resume) reads the same rows.
-- Keeping the high-churn agent-thread state in Postgres keeps it off the shared
-- cluster etcd, which is the whole reason the ADR rejected a CRD.
--
-- Snapshots are never load-bearing: durable task/conversation state lives in the
-- monolith elsewhere, so a lost snapshot degrades (re-init) rather than losing
-- work. The two snapshot refs are distinct roles:
--   base_snapshot_ref   - the warm base template a thread was created from
--                         (one per env-image version; instant ready start).
--   thread_snapshot_ref - this thread's own state at its last idle boundary.

CREATE TABLE claude_agent.agent_threads (
    thread_id            TEXT PRIMARY KEY,
    -- Lifecycle: PENDING -> RUNNING -> IDLE (pause+snapshot) -> RUNNING (restore)
    --            -> COMPLETED (reclaim) | FAILED.
    state                TEXT NOT NULL DEFAULT 'PENDING'
                             CHECK (state IN ('PENDING', 'RUNNING', 'IDLE', 'COMPLETED', 'FAILED')),
    repo                 TEXT NOT NULL DEFAULT '',
    branch               TEXT NOT NULL DEFAULT '',
    -- node + arch pin the thread: FC snapshots are non-portable and a mismatched
    -- restore fails closed, so the reconcile loop only acts on its own node.
    node                 TEXT NOT NULL DEFAULT '',
    arch                 TEXT NOT NULL DEFAULT '',
    base_snapshot_ref    TEXT,
    thread_snapshot_ref  TEXT,
    -- on-disk bundle size, used for GC budgeting against the nvme pool headroom.
    size_bytes           BIGINT NOT NULL DEFAULT 0,
    -- the Discord thread this agent thread fronts (ADR 021), if any.
    discord_thread       TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- idle TTL in seconds; the backstop/GC evicts idle threads past their TTL.
    ttl_secs             INTEGER NOT NULL DEFAULT 86400
);

-- The reconcile loop lists threads pinned to its node, ordered by recency.
CREATE INDEX idx_agent_threads_node
    ON claude_agent.agent_threads (node, last_active_at DESC);

-- GC sweeps by state + idleness to find eviction candidates.
CREATE INDEX idx_agent_threads_state
    ON claude_agent.agent_threads (state, last_active_at);
