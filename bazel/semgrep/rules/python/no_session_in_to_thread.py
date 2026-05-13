# Tests for no-session-in-to-thread rule.
# SQLAlchemy Sessions are not thread-safe and must not be passed to
# asyncio.to_thread(). Pass engine (or session.get_bind()) instead and
# create a new Session inside the threaded function. See PR #2297.
import asyncio

from sqlalchemy.orm import Session


# ruleid: no-session-in-to-thread
async def bad_session_first_extra_arg(engine, session):
    result = await asyncio.to_thread(sync_read, session)
    return result


# ruleid: no-session-in-to-thread
async def bad_session_with_extra_args(engine, session, item_id):
    result = await asyncio.to_thread(sync_read, item_id, session)
    return result


# ruleid: no-session-in-to-thread
async def bad_session_trailing(engine, session, item_id, limit):
    result = await asyncio.to_thread(sync_query, item_id, limit, session)
    return result


# ok: engine passed instead of session — caller creates Session inside thread
async def ok_engine_passed(engine, item_id):
    result = await asyncio.to_thread(sync_read_with_engine, engine, item_id)
    return result


# ok: db_session variable name does not match ^session$ regex
async def ok_db_session_name(engine, db_session, item_id):
    result = await asyncio.to_thread(sync_read, db_session, item_id)
    return result


# ok: no asyncio.to_thread call
async def ok_no_to_thread(engine, session, item_id):
    with Session(engine) as s:
        return s.get(MyModel, item_id)


# ok: session used in event loop, not passed to thread
async def ok_session_not_passed(engine, session):
    objs = session.query(MyModel).all()
    result = await asyncio.to_thread(cpu_bound_work, objs)
    return result


def sync_read(session, item_id=None):
    return session.query(MyModel).first()


def sync_read_with_engine(engine, item_id):
    with Session(engine) as session:
        return session.get(MyModel, item_id)


def sync_query(item_id, limit, session):
    return session.query(MyModel).limit(limit).all()


def cpu_bound_work(objs):
    return [o.value for o in objs]


class MyModel:
    pass
