-- Workers per WorkerPool, as the worker reaper last saw them: ready pods and
-- pods that host an actor. The connector reads this to hold a send until the
-- pool has a worker (worker pool autoscaling, ARCHITECTURE.md). The reaper
-- writes it only while it holds its lease, for the pools it is configured
-- with. No row, or a stale one, means "unknown" and the connector sends.
CREATE TABLE worker_pool_capacity (
    namespace text NOT NULL,
    pool text NOT NULL,
    ready integer NOT NULL CHECK (ready >= 0),
    busy integer NOT NULL CHECK (busy >= 0),
    observed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace, pool)
);
GRANT SELECT, INSERT, UPDATE ON worker_pool_capacity TO factory_reaper;
