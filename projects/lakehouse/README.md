# lakehouse — event-sourced pipeline

Purely-additive parallel stack to the existing orchestrator / `knowledge` path.
Source of truth: ADRs [`agents/015`](../../docs/decisions/agents/015-temporal-orchestration-substrate.md),
[`agents/016`](../../docs/decisions/agents/016-nats-canonical-event-stream.md),
[`agents/017`](../../docs/decisions/agents/017-domain-event-schema.md),
[`platform/004`](../../docs/decisions/platform/004-iceberg-lakehouse-hot-swap.md).

```
NATS JetStream  ->  Temporal workflows  ->  Iceberg on SeaweedFS  ->  DuckDB / Quack
(canonical          (durable                (immutable archive)       (stateless,
 event stream)       orchestration)                                    hot-swap serving)
```

## Layout

| Package             | Purpose                                                                | Wavefront |
| ------------------- | ---------------------------------------------------------------------- | --------- |
| `events/`           | Domain event envelope (ADR 017) + `publish_event` helpers              | 2         |
| `nats_client/`      | Async NATS JetStream wrapper + consumer-group helpers                  | 2         |
| `orchestrator/`     | Temporal client/worker; `workflows/`, `schedules/`                     | 2 / 3     |
| `iceberg/`          | PyIceberg writer helpers; per-domain `tables/`                         | 2         |
| `duckdb_query/`     | DuckDB + iceberg-extension query helpers                               | 2         |
| `dispatchers/`      | NATS → Temporal workflow dispatchers                                   | 4         |
| `image/`            | apko worker image (`python -m projects.lakehouse.orchestrator.workflows.run`) | 3         |
| `quack-server/`     | apko DuckDB/Quack serving image                                        | 3         |
| `chart/`, `deploy/` | Path-based Helm chart + ArgoCD Application                             | 4         |

## Conventions

- **Imports** are workspace-root absolute: `from projects.lakehouse.events import ...`
  (standalone-project convention; gazelle-managed BUILD files).
- **Additive only.** Nothing here reads or writes the existing
  `knowledge.notes`/`knowledge.chunks` tables except the backfill, which reads
  them **read-only via raw SQL** (no monolith model import).
- **Deploy** is path-based (`targetRevision: HEAD`), so changes auto-merge
  without an OCI version bump — unlike the monolith chart.

## Status

Per-unit implementation notes live in
[`docs/plans/event-sourced-impl-status/`](../../docs/plans/event-sourced-impl-status/).
