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
| `image/`            | `py3_image` (custom Bazel macro) worker image (`python -m projects.lakehouse.orchestrator.workflows.run`) | 3         |
| `quack-server/`     | `py3_image` (custom Bazel macro) DuckDB/Quack serving image            | 3         |
| `chart/`, `deploy/` | OCI-versioned Helm chart + ArgoCD Application                          | 4         |

## Conventions

- **Imports** are workspace-root absolute: `from projects.lakehouse.events import ...`
  (standalone-project convention; gazelle-managed BUILD files).
- **Additive only.** Nothing here reads or writes the existing
  `knowledge.notes`/`knowledge.chunks` tables except the backfill, which reads
  them **read-only via raw SQL** (no monolith model import).
- **Deploy** uses OCI-based versioned deployment: the chart is pulled from
  `ghcr.io/jomcgi/homelab/charts` at the semver `targetRevision` in
  `deploy/application.yaml` (currently `0.2.3`). A `$values` git source
  (`targetRevision: HEAD`) overlays environment-specific values from this repo,
  but the chart itself is version-pinned — a `Chart.yaml` bump (kept in sync by
  the `chart-version-bot`) is required to roll new images.

## Status

Per-unit implementation notes live in
[`docs/plans/event-sourced-impl-status/`](../../docs/plans/event-sourced-impl-status/).
