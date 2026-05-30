# WF-SCHEDULES — Temporal schedule definitions (Wavefront 3)

**Unit:** WF-SCHEDULES (Wavefront-3 fan-out)
**Branch / PR:** `feat/lakehouse-wf-schedules`
**Classification:** [auto] — all-new gazelle-managed package; no shared-file edits.

## What shipped

New package `projects/lakehouse/orchestrator/schedules/` (the unit owns the whole
dir, so its hand-written `BUILD` is not a cross-unit conflict):

- `__init__.py` — pkgutil loader + idempotent registration helper:
  - `ScheduleDefinition(schedule_id, schedule)` — frozen dataclass; `schedule` is
    a `temporalio.client.Schedule`.
  - `all_schedules() -> list[ScheduleDefinition]` — walks this package via
    `pkgutil.iter_modules`, aggregating each submodule's module-level `SCHEDULES`
    list. New schedule = new file, **no shared list to edit** (conflict-free,
    mirrors the `workflows` loader).
  - `register_schedules(client, *, namespace="default")` — async; for each
    discovered definition calls `client.create_schedule(id, schedule)`, swallowing
    `temporalio.client.ScheduleAlreadyRunningError`. Called from monolith/worker
    startup in Wavefront 5. (`temporalio` imported lazily inside the function so
    the hermetic loader stays import-light.)
- One file per schedule (each exports `SCHEDULES = [ScheduleDefinition(...)]`):
  - `gap_drain_sweep.py`, `iceberg_batch.py`, `build_serving.py`, `tag_rotation.py`.
- `schedules_test.py` — hermetic: discovery of all four, per-spec workflow
  type-name / cadence / task queue, and `register_schedules` against a mocked
  client (create-per-spec, AlreadyRunning swallowed, unexpected errors propagate).
- `BUILD` — hand-written following the sibling `orchestrator`/`events` gazelle
  pattern (`py_library` globbing all non-test srcs, one `py_test`, one
  `semgrep_test` per source). Includes the proven `# gazelle:resolve py
temporalio @pip//temporalio` (+ `.client`) directives because the generated
  module manifest (`bazel/tools/python/gazelle_python.yaml`) was never regenerated
  to include `temporalio` (same situation the W2/orchestrator package documents).

## The four schedules

| Schedule ID              | Workflow type-name             | Cadence             | Task queue        |
| ------------------------ | ------------------------------ | ------------------- | ----------------- |
| `gap-drain-sweep`        | `GapDrainSweepWorkflow`        | cron `*/5 * * * *`  | `gap-drain`       |
| `iceberg-batch-commit`   | `IcebergBatchCommitWorkflow`   | interval 90s        | `iceberg-builder` |
| `build-serving-artifact` | `BuildServingArtifactWorkflow` | cron `*/15 * * * *` | `housekeeping`    |
| `tag-rotation`           | `TagRotationWorkflow`          | cron `*/15 * * * *` | `housekeeping`    |

Iceberg batch commit uses an interval (`ScheduleIntervalSpec(every=90s)`) rather
than cron — sub-minute granularity isn't expressible in 5-field cron, and 90s
matches the "every 1-2 min" batch-commit intent. The other three use
`ScheduleSpec(cron_expressions=[...])`.

## Idempotency

Workflows are referenced **by type-name string**
(`ScheduleActionStartWorkflow(workflow="GapDrainSweepWorkflow", ...)`), so this
package imports neither the workflow classes nor the parallel WF-STORAGE /
WF-DOMAIN units. `register_schedules` is idempotent because `create_schedule`
raises `ScheduleAlreadyRunningError` when a schedule ID already exists (created on
a prior boot); the helper catches and skips it, so re-running registration on
every startup is a no-op for already-registered schedules. Unexpected errors
propagate.

## Deviations / notes

- The standalone `gazelle` binary in the dev-tools cache did not emit a BUILD for
  the new dir locally (it lacks the repo's full Python gazelle config, which is
  CI-only under `//bazel/tools/python`). BUILD was hand-written to the exact
  sibling pattern; ci-format-bot will normalize it on the PR if needed.
- `register_schedules` accepts `namespace` for surface symmetry and logs it;
  Temporal has no per-call namespace override on `create_schedule`, so the
  schedule is created in whatever namespace the `client` is connected to (callers
  connect the client to the target namespace).
