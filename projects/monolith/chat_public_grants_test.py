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
            # response_cache (added 20260619010000): public_writer must be able to
            # upsert and read it for the durable response cache to work.
            session.execute(
                text(
                    "INSERT INTO chat_public.response_cache "
                    "(cache_key, normalized_message, prompt_version, notes_watermark, "
                    "response_text) VALUES "
                    "('grant-test-key', 'q', 'pv', 'wm', 'a')"
                )
            )
            session.execute(
                text(
                    "SELECT count(*) FROM chat_public.response_cache "
                    "WHERE cache_key = 'grant-test-key'"
                )
            ).scalar_one()
            session.execute(
                text(
                    "DELETE FROM chat_public.response_cache WHERE cache_key = 'grant-test-key'"
                )
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


def test_public_reader_can_read_shared_snapshots(pg):
    """The share read route (GET /shared/{id}, ADR 005) runs as public_reader on
    the replica, so public_reader MUST be able to SELECT a snapshot that
    public_writer minted. Regression for the missing GRANT USAGE ON SCHEMA
    chat_public TO public_reader: without schema USAGE the table SELECT was dead
    and every share link 404'd (the SSR loader collapsed the permission error)."""
    engine = create_engine(pg.url)
    try:
        with Session(engine) as session:
            # Mint a snapshot as the writer role. source_session_id is nullable,
            # so no session row is needed to exercise the read grant.
            session.execute(text("SET ROLE public_writer"))
            session.execute(
                text(
                    "INSERT INTO chat_public.shared_snapshots "
                    "(id, transcript, message_count) "
                    "VALUES ('grant-test-snap', '[]'::jsonb, 0)"
                )
            )
            session.execute(text("RESET ROLE"))

            # Read it back as the read-only public_reader role.
            session.execute(text("SET ROLE public_reader"))
            count = session.execute(
                text(
                    "SELECT count(*) FROM chat_public.shared_snapshots "
                    "WHERE id = 'grant-test-snap'"
                )
            ).scalar_one()
            assert count == 1
            # Never commit: the row and the role changes are rolled back.
            session.rollback()
    finally:
        engine.dispose()


def test_public_reader_cannot_read_chat_transcripts(pg):
    """Schema USAGE on chat_public does not leak conversations: public_reader has
    table SELECT only on shared_snapshots, so reading sessions or messages still
    raises permission denied. Keeps the share-read grant least-privilege (no
    transcripts, IP/UA hashes, or response cache exposed to the public reader)."""
    engine = create_engine(pg.url)
    try:
        with Session(engine) as session:
            for tbl in ("sessions", "messages", "response_cache"):
                # Re-set the role each iteration: the prior aborted statement is
                # rolled back, which also unwinds the SET ROLE.
                session.execute(text("SET ROLE public_reader"))
                with pytest.raises(Exception) as exc:
                    session.execute(
                        text(f"SELECT count(*) FROM chat_public.{tbl}")
                    ).all()
                assert "permission denied" in str(exc.value).lower()
                session.rollback()
    finally:
        engine.dispose()
