"""Phase 2 (ADR 004 security/004): public_reader is a least-privilege read role.

Exercises the GRANTs from chart/migrations/20260617000000_public_reader_role.sql
against a real Postgres (the `pg` fixture applies every migration), using SET ROLE
so no login credential is needed. The point of the role is confidentiality at the
database layer: private rows must be unreachable even with full query access.
"""

import pytest
from sqlmodel import Session, create_engine, text


def _seed_notes(engine) -> None:
    with Session(engine) as session:
        session.execute(
            text(
                """
                INSERT INTO knowledge.notes
                    (note_id, path, title, content_hash, content, visibility)
                VALUES
                    ('pubnote', 'pubnote.md', 'Public', 'h1', 'public body', 'public'),
                    ('privnote', 'privnote.md', 'Private', 'h2', 'secret body', 'private')
                ON CONFLICT (note_id) DO NOTHING
                """
            )
        )
        session.commit()


def test_public_reader_view_exposes_only_public_notes(pg):
    engine = create_engine(pg.url)
    _seed_notes(engine)
    try:
        with Session(engine) as session:
            session.execute(text("SET ROLE public_reader"))
            rows = session.execute(
                text("SELECT note_id FROM public_api.knowledge_notes ORDER BY note_id")
            ).all()
            assert [r[0] for r in rows] == ["pubnote"]
    finally:
        engine.dispose()


def test_public_reader_denied_on_private_knowledge_table(pg):
    engine = create_engine(pg.url)
    try:
        with Session(engine) as session:
            session.execute(text("SET ROLE public_reader"))
            with pytest.raises(Exception) as exc:
                session.execute(text("SELECT note_id FROM knowledge.notes")).all()
            assert "permission denied" in str(exc.value).lower()
    finally:
        engine.dispose()


def test_public_reader_can_read_public_datasets(pg):
    engine = create_engine(pg.url)
    try:
        with Session(engine) as session:
            session.execute(text("SET ROLE public_reader"))
            # A granted SELECT on each wholly-public dataset must not raise.
            for table in (
                "ships.vessels",
                "stars.sites",
                "hikes.walks",
                "dr_jobs.nhs_vacancies",
            ):
                session.execute(text(f"SELECT count(*) FROM {table}"))
    finally:
        engine.dispose()
