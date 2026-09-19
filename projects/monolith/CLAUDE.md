# CLAUDE.md - monolith

Scoped guidance for `projects/monolith`. The repo-root `CLAUDE.md` still applies.
See `README.md` in this directory for the domain map and public/private tier boundary.

## Scheduled job registrations are metadata only

Handlers registered with `scheduler.api.register_job` have the signature
`async def handler(session: Session) -> datetime | None`, but the in-process
dispatcher was deleted. Registration now populates only legacy scheduler rows
and handler metadata for views and orphan checks. Scheduled execution comes from
Argo CronWorkflows invoking `app/jobs_main.py`; `replaces` adds a name to
`ARGO_JOBS` and suppresses that metadata even when the CronWorkflow is suspended.

Async batch handlers must still keep synchronous SQLModel work off their event
loop. Semgrep `no-sync-session-in-async-def` fails CI on direct Session methods
inside `async def`, and `no-session-in-to-thread` rejects moving an existing
Session across threads.

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
   (semgrep `no-session-in-to-thread` blocks that, and a session is not safe to
   use across threads).
4. Keep the DB logic in a sync core that takes an explicit `session` parameter
   so the SQLite `create_all` test fixtures can drive it directly; the async
   wrapper (network + `to_thread`) stays thin and is not unit tested.
5. Do not `session.add` in a loop (semgrep `session-add-in-loop`): build the
   rows and `session.add_all(...)` once, or mutate `session.get`-tracked rows
   and let them flush on `commit`.
6. Put memory requests, limits, deadlines, and concurrency policy on the Argo
   CronWorkflow. `register_job(..., heavy=True)` is legacy metadata and does not
   serialize execution.

## Test fixtures use SQLite; datetimes come back naive

Model/endpoint tests use SQLite + `SQLModel.metadata.create_all` (not
migrations). SQLite has no tz-aware type, so a `TIMESTAMPTZ` column round-trips
as a **naive** datetime in tests, while production (Postgres) is tz-aware.

- Assert `isinstance(value, datetime)`, not `value.tzinfo is not None`.
- When comparing loaded datetimes for equality (sets, `==`), coerce first:
  `dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)`.
- Serialize datetimes through an `_as_utc`/`_iso` helper (see `ships/router.py`)
  so JSON output and ETags are offset-consistent across SQLite tests and
  Postgres prod.
