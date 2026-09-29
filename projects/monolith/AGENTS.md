# AGENTS.md - monolith

Scoped guidance for `projects/monolith`, for any agent. The repo-root `AGENTS.md` still applies.
See `README.md` in this directory for the domain map and public/private tier boundary.

## Scheduled job registrations are metadata only

Handlers registered with `scheduler.api.register_job` have the signature
`async def handler(session: Session) -> datetime | None`, but the in-process
dispatcher was deleted. Registration now populates only legacy scheduler rows
and handler metadata for views and orphan checks. Scheduled execution comes from
Argo CronWorkflows invoking `app/jobs_main.py`; `replaces` adds a name to
`ARGO_JOBS` and suppresses that metadata even when the CronWorkflow is suspended.

Async batch handlers must still keep synchronous SQLModel work off their event
loop: no direct Session methods inside `async def`, and never move an existing
Session across threads. Both are gated for new code by
`bazel/tools/ci/source_ratchet.py` (`sync-session`, `session-to-thread`); older
instances are grandfathered, so do not copy a pattern just because it exists.

The established pattern (see `hikes/jobs.py`, `ships/retention.py`):

1. Do all network I/O in the async handler with `await` first.
2. Delegate **all** Session I/O to a worker thread:
   `await asyncio.to_thread(_sync_helper, data)`.
3. The sync helper opens its **own** fresh session and commits:
   ```python
   def _sync_helper(data) -> int:
       from core.db import get_engine
       with Session(get_engine()) as session:
           ...  # sync DB work
           session.commit()
   ```
   Pass plain data into `to_thread`, **never** the handler's `session` argument
   (a session is not safe to use across threads).
4. Keep the DB logic in a sync core that takes an explicit `session` parameter
   so the SQLite `create_all` test fixtures can drive it directly; the async
   wrapper (network + `to_thread`) stays thin and is not unit tested.
5. Do not `session.add` in a loop (gated: `session-add-loop`): build the
   rows and `session.add_all(...)` once, or mutate `session.get`-tracked rows
   and let them flush on `commit`.
6. Put memory requests, limits, deadlines, and concurrency policy on the Argo
   CronWorkflow. `register_job(..., heavy=True)` is legacy metadata and does not
   serialize execution.

## Test-writing traps

- Nearly every subpackage here is gazelle-excluded
  (`grep gazelle:exclude projects/monolith/BUILD`): a new `*_test.py` needs a
  hand-written `py_test` in `projects/monolith/BUILD` or it never runs.
- Model and endpoint tests use SQLite plus `SQLModel.metadata.create_all`, not
  migrations. Use a file-backed database under `tmp_path`: an in-memory
  StaticPool database is one connection and deadlocks concurrency tests.
- SQLite has no tz-aware type, so a `TIMESTAMPTZ` column comes back **naive**
  in tests while Postgres is tz-aware. Assert `isinstance(value, datetime)`,
  not `value.tzinfo is not None`; coerce before comparing
  (`dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)`); serialize through
  an `_as_utc`/`_iso` helper (see `ships/router.py`) so JSON and ETags match
  across SQLite and Postgres.
- `build_app()` calls `logging.basicConfig(force=True)`, which removes
  pytest's caplog handler: re-add it after `build_app`.
- Mock async callables with async functions; a sync lambda fails only at
  runtime.
- Assert on ORM objects inside the session context; afterwards attribute
  access lazy-loads and throws.
- Never monkeypatch a builtin through a module attribute (`module.open`);
  patch `builtins.open` or restructure the seam.
