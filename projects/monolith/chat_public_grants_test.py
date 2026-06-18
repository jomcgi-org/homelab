"""Phase 1 (ADR 005): the public_writer role is scoped to the chat_public schema.

Exercises the GRANTs from chart/migrations/20260617030000_chat_public.sql against
a real Postgres (the `pg` fixture applies every migration), using SET ROLE so no
login credential is needed. The point of the role split is the database half of
the read/write isolation: the public_writer role may DML the chat_public schema
and nothing else, and the read-only public_reader role may never write that
schema.

Hand-written bdd_test (real DB), so excluded from gazelle and registered by hand
in projects/monolith/BUILD.
"""

import pytest
from sqlmodel import Session, create_engine, text


def test_public_writer_role_can_dml_chat_public_schema(pg):
    """SET ROLE public_writer; round-trip a session + message (INSERT/SELECT/
    UPDATE/DELETE) on the chat_public schema. Run inside a transaction that is
    rolled back, so the session-scoped Postgres stays clean for other tests."""
    engine = create_engine(pg.url)
    try:
        with Session(engine) as session:
            session.execute(text("SET ROLE public_writer"))

            session.execute(
                text(
                    "INSERT INTO chat_public.sessions (id, status) "
                    "VALUES ('grant-test-sess', 'active')"
                )
            )
            session.execute(
                text(
                    "INSERT INTO chat_public.messages (session_id, role, content) "
                    "VALUES ('grant-test-sess', 'user', 'hello')"
                )
            )

            count = session.execute(
                text(
                    "SELECT count(*) FROM chat_public.messages "
                    "WHERE session_id = 'grant-test-sess'"
                )
            ).scalar_one()
            assert count == 1

            session.execute(
                text(
                    "UPDATE chat_public.sessions SET turn_count = 1 "
                    "WHERE id = 'grant-test-sess'"
                )
            )
            turn_count = session.execute(
                text(
                    "SELECT turn_count FROM chat_public.sessions "
                    "WHERE id = 'grant-test-sess'"
                )
            ).scalar_one()
            assert turn_count == 1

            session.execute(
                text(
                    "DELETE FROM chat_public.messages WHERE session_id = 'grant-test-sess'"
                )
            )
            session.execute(
                text("DELETE FROM chat_public.sessions WHERE id = 'grant-test-sess'")
            )
            # Never commit: the role can do the work, and nothing persists.
            session.rollback()
    finally:
        engine.dispose()


def test_public_writer_role_denied_on_other_schemas(pg):
    """The write role is scoped to chat_public only: reading another schema
    (knowledge.notes) raises permission denied."""
    engine = create_engine(pg.url)
    try:
        with Session(engine) as session:
            session.execute(text("SET ROLE public_writer"))
            with pytest.raises(Exception) as exc:
                session.execute(text("SELECT note_id FROM knowledge.notes")).all()
            assert "permission denied" in str(exc.value).lower()
    finally:
        engine.dispose()


def test_public_reader_cannot_write_chat_public(pg):
    """public_reader stays read-only: inserting into chat_public.sessions raises
    permission denied, so the read tier can never become a chat writer."""
    engine = create_engine(pg.url)
    try:
        with Session(engine) as session:
            session.execute(text("SET ROLE public_reader"))
            with pytest.raises(Exception) as exc:
                session.execute(
                    text(
                        "INSERT INTO chat_public.sessions (id, status) "
                        "VALUES ('reader-should-fail', 'active')"
                    )
                )
            assert "permission denied" in str(exc.value).lower()
    finally:
        engine.dispose()
