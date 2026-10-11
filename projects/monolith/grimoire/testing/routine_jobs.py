"""SQLite queue fixture for session-end enqueue and claim-owner tests."""

from sqlalchemy import text


def create_routine_jobs_table(session):
    session.execute(
        text("""CREATE TABLE IF NOT EXISTS routine_jobs (
        name TEXT PRIMARY KEY, routine_kind TEXT, interval_secs INTEGER,
        next_run_at TEXT, last_run_at TEXT, last_status TEXT, last_summary TEXT,
        locked_by TEXT, locked_at TEXT, ttl_secs INTEGER, payload TEXT,
        created_by TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    )
    session.commit()
