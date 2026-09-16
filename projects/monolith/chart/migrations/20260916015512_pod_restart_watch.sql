-- pod_restart_watch is a last-observation-wins table, not an audit log: one
-- row per live container records its latest Kubernetes restart count.
-- The writer is the private tier app role, so no explicit GRANT is needed.

CREATE TABLE pod_restart_watch (
    namespace TEXT NOT NULL,
    pod TEXT NOT NULL,
    container TEXT NOT NULL,
    restart_count INTEGER NOT NULL,
    last_reason TEXT,
    observed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (namespace, pod, container)
);
