# Tests for no-sync-session-in-async-def rule.
# Synchronous SQLAlchemy Session I/O (execute, scalars, commit, flush) called
# directly inside an async def blocks the event loop. Offload to
# asyncio.to_thread() with a fresh Session created inside the thread.
# See PR #2297.
import asyncio

from sqlalchemy import select
from sqlalchemy.orm import Session


# ruleid: no-sync-session-in-async-def
async def bad_execute(engine, session, item_id):
    result = session.execute(select(MyModel).where(MyModel.id == item_id))
    return result.scalars().first()


# ruleid: no-sync-session-in-async-def
async def bad_scalars(engine, session, item_id):
    result = session.scalars(select(MyModel).where(MyModel.id == item_id))
    return result.all()


# ruleid: no-sync-session-in-async-def
async def bad_commit(engine, session):
    session.add(MyModel(name="test"))
    session.commit()


# ruleid: no-sync-session-in-async-def
async def bad_flush(engine, session):
    session.add(MyModel(name="test"))
    session.flush()


# ok: sync def — not an async function, blocking I/O is expected
def ok_sync_execute(session, item_id):
    result = session.execute(select(MyModel).where(MyModel.id == item_id))
    return result.scalars().first()


# ok: sync def — commit/flush in sync context is fine
def ok_sync_commit(session):
    session.add(MyModel(name="test"))
    session.commit()


# ok: offloads to asyncio.to_thread with a fresh Session
async def ok_with_to_thread(engine, item_id):
    def _sync_fn(engine, item_id):
        with Session(engine) as session:
            return (
                session.execute(select(MyModel).where(MyModel.id == item_id))
                .scalars()
                .first()
            )

    return await asyncio.to_thread(_sync_fn, engine, item_id)


# ok: session.query() is not in the flagged method list
async def ok_session_query_not_flagged(session):
    return session.query(MyModel).all()


# ok: method on a different object — not session
async def ok_other_object_execute(db, query):
    result = db.execute(query)
    return result


class MyModel:
    id: int
    name: str
