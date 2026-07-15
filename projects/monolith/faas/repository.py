"""Thin repository over faas.function: the durable half of ADR 045.

ORM-level get-or-update (not a dialect-specific ``INSERT ... ON CONFLICT``) so
this module runs identically on SQLite (unit tests, ``SQLModel.metadata.create_all``)
and Postgres (production), mirroring ships/store.py's Vessel upsert. Every
function is called with an explicit ``Session`` (mirrors agent/locks.py /
ships/store.py): callers own the session and commit boundary.

Global name uniqueness is the PK; visibility is a flag, not a namespace (ADR
045). ``last_smoke_at`` doubles as the visibility gate: NULL means registered
but not yet smoke-passed (not visible); a set value means the current zip
passed its test-run gate (Task 10) and is servable (Task 11 filters on it).
Re-registering a name is last-write-wins and resets ``last_smoke_at`` to NULL
until the new zip smokes again.
"""

from datetime import datetime, timezone

from sqlmodel import Session, select

from faas.models import Function


def upsert_function(
    session: Session,
    *,
    name: str,
    visibility: str,
    runtime: str,
    handler: str,
    zip_sha256: str,
    code_uri: str,
    created_by: str | None,
) -> Function:
    """Create or last-write-wins replace a function row by name.

    Re-registering an existing name overwrites every mutable column and resets
    ``last_smoke_at`` to NULL: the freshly-registered zip is not yet visible
    until it passes the test-run gate again (Task 10 calls ``mark_smoked``).
    """
    existing = session.get(Function, name)
    now = datetime.now(timezone.utc)
    if existing is None:
        function = Function(
            name=name,
            visibility=visibility,
            runtime=runtime,
            handler=handler,
            zip_sha256=zip_sha256,
            code_uri=code_uri,
            created_by=created_by,
            created_at=now,
            updated_at=now,
            last_smoke_at=None,
        )
        session.add(function)
        session.commit()
        session.refresh(function)
        return function

    existing.visibility = visibility
    existing.runtime = runtime
    existing.handler = handler
    existing.zip_sha256 = zip_sha256
    existing.code_uri = code_uri
    existing.created_by = created_by
    existing.updated_at = now
    existing.last_smoke_at = None
    session.commit()
    session.refresh(existing)
    return existing


def mark_smoked(session: Session, name: str) -> None:
    """Flip the visibility gate: the current zip just passed its smoke test."""
    function = session.get(Function, name)
    if function is None:
        return
    function.last_smoke_at = datetime.now(timezone.utc)
    session.commit()


def get_function(session: Session, name: str) -> Function | None:
    """Return the function row regardless of visibility, or None."""
    return session.get(Function, name)


def get_visible_function(session: Session, name: str) -> Function | None:
    """Return the function row only if it has passed its smoke test."""
    function = session.get(Function, name)
    if function is None or function.last_smoke_at is None:
        return None
    return function


def list_functions(session: Session, *, visible_only: bool = False) -> list[Function]:
    """List all functions, optionally filtered to smoke-passed (visible) ones."""
    stmt = select(Function)
    if visible_only:
        stmt = stmt.where(Function.last_smoke_at.is_not(None))
    return list(session.exec(stmt).all())


def delete_function(session: Session, name: str) -> bool:
    """Delete a function row by name. Returns whether a row was deleted."""
    function = session.get(Function, name)
    if function is None:
        return False
    session.delete(function)
    session.commit()
    return True
