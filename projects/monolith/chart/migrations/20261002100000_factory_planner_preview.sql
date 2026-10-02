-- Each planner invocation may evaluate at most two proposed graph decisions.
-- This ledger is the only write the advisory preview adapter makes.
CREATE TABLE swarm.factory_planner_preview (
    id BIGSERIAL PRIMARY KEY,
    planner_run_id INTEGER NOT NULL REFERENCES swarm.swarm_node_run(id),
    ordinal INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT factory_planner_preview_run_ordinal UNIQUE (planner_run_id, ordinal),
    CONSTRAINT factory_planner_preview_ordinal CHECK (ordinal IN (1, 2))
);
