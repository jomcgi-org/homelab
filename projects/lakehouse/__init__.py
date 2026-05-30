"""Event-sourced lakehouse pipeline (ADRs agents/015, agents/016, agents/017, platform/004).

A purely-additive parallel stack to the existing orchestrator / knowledge path:

  NATS JetStream (canonical event stream)
    -> Temporal workflows (durable orchestration)
    -> Iceberg on SeaweedFS (immutable archive)
    -> DuckDB / Quack (stateless, hot-swappable serving)

Sub-packages (each its own Wavefront-2 unit, imported as
``projects.lakehouse.<pkg>``):

  events        domain event envelope (ADR 017) + publish helpers
  nats_client   async NATS JetStream wrapper
  orchestrator  Temporal client/worker + workflows/schedules (Wavefronts 2-3)
  iceberg       PyIceberg writer helpers + per-domain table definitions
  duckdb_query  DuckDB + iceberg-extension query helpers
  dispatchers   NATS -> Temporal workflow dispatchers (Wavefront 4)

Nothing here reads or writes the existing notes/embedding tables; the backfill
(Wavefront 5) reads ``knowledge.notes``/``knowledge.chunks`` read-only via raw SQL.
"""
