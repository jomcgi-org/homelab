-- ADR agents/038 verdict feedback (#3843). The original task class owns the
-- rolling window. routing_tier is a per-admission pin, so a class transition
-- cannot change the completion contract of work already in flight.
ALTER TABLE swarm.factory_receipt
  ADD COLUMN routing_tier TEXT;

ALTER TABLE swarm.factory_receipt
  ADD CONSTRAINT factory_receipt_routing_tier_check
  CHECK (routing_tier IS NULL OR routing_tier IN ('delivery', 'advisory'));

CREATE TABLE swarm.factory_class_tier (
    task_class TEXT PRIMARY KEY,
    routing_tier TEXT NOT NULL DEFAULT 'delivery',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT factory_class_tier_value_check
      CHECK (routing_tier IN ('delivery', 'advisory'))
);

CREATE TABLE swarm.factory_review_verdict (
    id BIGSERIAL PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES swarm.swarm_task (id),
    review_run_id BIGINT NOT NULL REFERENCES swarm.swarm_node_run (id),
    task_class TEXT NOT NULL,
    verdict TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    head_sha TEXT,
    reviewed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT factory_review_verdict_task_id_key UNIQUE (task_id),
    CONSTRAINT factory_review_verdict_review_run_id_key UNIQUE (review_run_id),
    CONSTRAINT factory_review_verdict_value_check
      CHECK (verdict IN ('approve', 'changes_requested'))
);

CREATE INDEX factory_review_verdict_class_reviewed_idx
  ON swarm.factory_review_verdict (task_class, reviewed_at, id);
