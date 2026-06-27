-- Adds claude_agent.agent_base_snapshots: the registry of per-repo warm bases
-- (ADR 022, Phase 4). A base is a microVM booted from a repo's env image with
-- main checked out and the harness warmed, snapshotted once; new threads restore
-- from it for an instant ready start instead of a cold boot + full clone.
--
-- Desired-vs-actual, same as the thread registry: requested_sha is the main
-- commit the base SHOULD be built at (a monolith routine bumps it when a repo's
-- main advances); built_sha is what the controller has actually built. When they
-- differ, fc-agentd rebuilds the base. In-flight idle threads carry their own
-- thread snapshots and are untouched by a base refresh.
--
-- A base is keyed by (repo, arch): snapshots are CPU-arch-bound, and there is one
-- warm base per repo env-image version per arch. base_ref is the opaque key the
-- controller stores the bundle under and threads reference as base_snapshot_ref.

CREATE TABLE claude_agent.agent_base_snapshots (
    base_ref       TEXT PRIMARY KEY,
    repo           TEXT NOT NULL,
    arch           TEXT NOT NULL,
    node           TEXT NOT NULL DEFAULT '',
    -- the main commit this base should be / was built at.
    requested_sha  TEXT NOT NULL DEFAULT '',
    built_sha      TEXT,
    size_bytes     BIGINT NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    built_at       TIMESTAMPTZ,
    UNIQUE (repo, arch)
);

-- The reconcile loop scans for bases whose built_sha lags requested_sha.
CREATE INDEX idx_agent_base_snapshots_stale
    ON claude_agent.agent_base_snapshots (repo, arch)
    WHERE built_sha IS DISTINCT FROM requested_sha;
