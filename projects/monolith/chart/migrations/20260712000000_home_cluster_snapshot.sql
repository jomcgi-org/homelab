-- home.cluster_snapshot: single-row snapshot of the cluster health rollup and
-- the firing SigNoz alerts shown on the private dashboard. Mirrors
-- home.calendar_snapshot (20260622120000).
--
-- The dashboard used to recompute both sections on every request: a live scan
-- of ~235 Kubernetes resources (pods, deployments, statefulsets, daemonsets,
-- ArgoCD apps) across all namespaces, plus a SigNoz /api/v1/rules fetch, with
-- no caching. A cold page load therefore blocked on the whole scan, up to the
-- SSR timeout. A background job (home.cluster_snapshot_refresh) now runs the
-- scan and fetch at a cadence and upserts this one row, so the request path is
-- a single-row read.
--
-- Single row (id = 1). health and alerts are stored verbatim as the read path
-- returns them (health is build_health() output; alerts is {"firing": [...]}).
-- snapshot_at lets the read path detect a wedged refresher and fall back to a
-- live scan rather than serve hours-stale "all healthy".

CREATE SCHEMA IF NOT EXISTS home;

CREATE TABLE home.cluster_snapshot (
    id          SMALLINT PRIMARY KEY DEFAULT 1,
    health      JSONB NOT NULL,
    alerts      JSONB NOT NULL,
    snapshot_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT cluster_snapshot_singleton CHECK (id = 1)
);
