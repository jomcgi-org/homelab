# W3-PREP — workflows package scaffold (Wavefront-3 prerequisite)

**Unit:** W3-PREP (orchestrator-authored, serialized before the Wavefront-3 fan-out)
**Classification:** [manual-review] — one existing-file edit (`orchestrator/BUILD` directive).

## Why this exists (not a plan unit)

The plan puts each workflow in its own file under `orchestrator/workflows/<id>.py`
with pkgutil discovery and "no shared registration list." But gazelle generates
**one BUILD per directory** (one `py_test` per `*_test.py`), so five parallel
workflow units would each regenerate `workflows/BUILD` and collide. This unit
creates the shared seam **once** so the workflow units are genuinely conflict-free.

## What shipped (all new files except one directive edit)

- `orchestrator/workflows/__init__.py` — pkgutil loader: `discover_workflows()` /
  `discover_activities()` walk this package's modules and read each module's
  `WORKFLOWS` / `ACTIVITIES` attributes. No shared list to edit.
- `orchestrator/workflows/run.py` — worker entrypoint
  (`python -m projects.lakehouse.orchestrator.workflows.run`): reads `TASK_QUEUE`,
  discovers workflows/activities, runs `orchestrator.worker.run_worker`. The
  Wavefront-3 worker image uses this as its command. (Note: `worker.py` is NOT
  modified — discovery lives in this new module, so the merged W2 worker skeleton
  and its tests stay untouched.)
- `orchestrator/workflows/BUILD` — **hand-written, gazelle-excluded**, glob-based:
  `py_library workflows` (glob srcs), `py_venv_binary worker_main` (main=run.py),
  one glob'd `py_test workflows_test`, and `semgrep_test`. `deps` is a **superset**
  covering every lakehouse workflow (events, nats_client, iceberg, duckdb_query,
  orchestrator + @pip temporalio/pyiceberg/pyarrow/duckdb/psycopg/pydantic).
- `orchestrator/workflows/loader_test.py` — hermetic loader tests.
- `orchestrator/BUILD` — added `# gazelle:exclude workflows` (the one existing-file
  edit) so gazelle leaves the hand-written `workflows/BUILD` alone.

## Convention for Wavefront-3 workflow units (READ THIS)

Each workflow unit adds, under `orchestrator/workflows/`, **exactly**:

- `<workflow_id>.py` exporting module-level `WORKFLOWS = [...]` (the
  `@temporalio.workflow.defn` classes) and optional `ACTIVITIES = [...]`.
- `<workflow_id>_test.py` (hermetic).

**Touch NO BUILD file** — the globs pick the new files up and `deps` already covers
them. If a workflow genuinely needs a dep not in the `_WORKFLOW_DEPS` superset,
STOP and flag it (it's a serialized `workflows/BUILD` edit, not a parallel-safe change).
gazelle does not run here, so no `# gazelle:resolve` directives are needed — deps
are explicit `@pip//...` labels.

## Deviations / notes

- Skipped the `gazelle_python.yaml` manifest regen (risky `bazel run`, huge diff).
  Because `workflows/` is gazelle-excluded with hand-written explicit `@pip//`
  deps, gazelle's stale module manifest is irrelevant here.
- Workflows are flat files (per the plan), not subpackages — the glob'd BUILD makes
  that conflict-free without subpackage nesting.
