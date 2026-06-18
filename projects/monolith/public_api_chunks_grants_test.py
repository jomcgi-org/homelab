"""Phase 4a (ADR 005 layer 5): the public chunk view confines retrieval to public.

Exercises the GRANTs + view from chart/migrations/20260617040000_public_api_chunks.sql
against a real Postgres (the `pg` fixture applies every migration), using SET ROLE so
no login credential is needed. The point is confidentiality at the database layer:
the public-chat retrieval path reads ONLY chunks of public notes, so a private note's
chunk text and embedding are physically unreachable as public_reader regardless of any
prompt or query. This is the database half of "confinement is a DB property, never a
prompt instruction".

Hand-written bdd_test (real DB), so excluded from gazelle and registered by hand in
projects/monolith/BUILD.
"""

import pytest
from sqlmodel import Session, create_engine, text

# A 1024-dim pgvector literal (matches knowledge.chunks.embedding's Vector(1024)).
# The exact direction is irrelevant: this test asserts row visibility, not ranking.
_EMB = "[" + ",".join(["0.1"] * 1024) + "]"


def _seed(engine) -> None:
    """Seed one public and one private note, each with a chunk + embedding.

    Runs as the migration/owner role (the fixture connects as a superuser), so the
    INSERTs are allowed; the confinement assertions below run under SET ROLE
    public_reader. Idempotent via ON CONFLICT so a shared session-scoped Postgres
    stays usable across tests.
    """
    with Session(engine) as session:
        session.execute(
            text(
                """
                INSERT INTO knowledge.notes
                    (note_id, path, title, content_hash, content, visibility)
                VALUES
                    ('pub-chunk-note', 'pub-chunk-note.md', 'Public Chunk Note',
                     'hc1', 'public body', 'public'),
                    ('priv-chunk-note', 'priv-chunk-note.md', 'Private Chunk Note',
                     'hc2', 'secret body', 'private')
                ON CONFLICT (note_id) DO NOTHING
                """
            )
        )
        for note_id, ctext in (
            ("pub-chunk-note", "PUBLIC chunk grounding text"),
            ("priv-chunk-note", "PRIVATE secret chunk text"),
        ):
            session.execute(
                text(
                    """
                    INSERT INTO knowledge.chunks
                        (note_fk, chunk_index, section_header, chunk_text, embedding)
                    SELECT id, 0, '', :ctext, CAST(:emb AS vector)
                    FROM knowledge.notes
                    WHERE note_id = :nid
                      AND NOT EXISTS (
                          SELECT 1 FROM knowledge.chunks c
                          WHERE c.note_fk = knowledge.notes.id AND c.chunk_index = 0
                      )
                    """
                ),
                {"ctext": ctext, "emb": _EMB, "nid": note_id},
            )
        session.commit()


def _seed_repo_doc(engine) -> None:
    """Seed one repo doc + chunk (the machine-synced, public-grounding tier).

    Runs as the migration/owner role like _seed above. Idempotent via ON CONFLICT
    and a NOT EXISTS guard so the shared session-scoped Postgres stays reusable.
    """
    with Session(engine) as session:
        session.execute(
            text(
                """
                INSERT INTO knowledge.repo_docs (path, content_hash, title)
                VALUES ('docs/test-repo-doc.md', 'rh1', 'Test Repo Doc')
                ON CONFLICT (path) DO NOTHING
                """
            )
        )
        session.execute(
            text(
                """
                INSERT INTO knowledge.repo_doc_chunks
                    (repo_doc_fk, chunk_index, section_header, chunk_text, embedding)
                SELECT id, 0, 'Intro', :ctext, CAST(:emb AS vector)
                FROM knowledge.repo_docs
                WHERE path = 'docs/test-repo-doc.md'
                  AND NOT EXISTS (
                      SELECT 1 FROM knowledge.repo_doc_chunks rc
                      WHERE rc.repo_doc_fk = knowledge.repo_docs.id
                        AND rc.chunk_index = 0
                  )
                """
            ),
            {"ctext": "REPO DOC grounding text", "emb": _EMB},
        )
        session.commit()


def test_public_chunk_view_includes_repo_docs(pg):
    """As public_reader, the chunk view also returns repo-doc chunks surfaced via the
    UNION ALL: a synthetic note_id 'repo:'||path, the doc title, the chunk text, and a
    1024-dim embedding. The private note's chunk text stays unreachable through the
    same view (confinement holds across both arms of the union)."""
    engine = create_engine(pg.url)
    _seed(engine)
    _seed_repo_doc(engine)
    try:
        with Session(engine) as session:
            session.execute(text("SET ROLE public_reader"))
            row = session.execute(
                text(
                    "SELECT note_id, title, chunk_text, vector_dims(embedding) "
                    "FROM public_api.knowledge_chunks "
                    "WHERE note_id = 'repo:docs/test-repo-doc.md'"
                )
            ).one()
            assert row[0] == "repo:docs/test-repo-doc.md"
            assert row[1] == "Test Repo Doc"
            assert row[2] == "REPO DOC grounding text"
            assert row[3] == 1024
            # The private note's chunk text remains unreachable via the same view.
            chunk_texts = [
                r[0]
                for r in session.execute(
                    text("SELECT chunk_text FROM public_api.knowledge_chunks")
                ).all()
            ]
            assert "PRIVATE secret chunk text" not in chunk_texts
    finally:
        engine.dispose()


def test_public_chunk_view_exposes_only_public_note_chunks(pg):
    """As public_reader, the chunk view returns the public note's chunk and NOT the
    private note's chunk (its text never appears), even though both exist in the
    underlying knowledge.chunks table."""
    engine = create_engine(pg.url)
    _seed(engine)
    try:
        with Session(engine) as session:
            session.execute(text("SET ROLE public_reader"))
            rows = session.execute(
                text(
                    "SELECT note_id, chunk_text "
                    "FROM public_api.knowledge_chunks "
                    "WHERE note_id IN ('pub-chunk-note', 'priv-chunk-note') "
                    "ORDER BY note_id"
                )
            ).all()
            note_ids = [r[0] for r in rows]
            chunk_texts = [r[1] for r in rows]
            assert note_ids == ["pub-chunk-note"]
            assert "PRIVATE secret chunk text" not in chunk_texts
            # The embedding column is exposed for the cosine search.
            emb = session.execute(
                text(
                    "SELECT embedding FROM public_api.knowledge_chunks "
                    "WHERE note_id = 'pub-chunk-note'"
                )
            ).scalar_one()
            assert emb is not None
    finally:
        engine.dispose()


def test_public_reader_denied_on_underlying_knowledge_chunks(pg):
    """public_reader has no access to the knowledge schema: reading the underlying
    knowledge.chunks table directly raises permission denied, so the only path to
    any chunk is the public-only view."""
    engine = create_engine(pg.url)
    try:
        with Session(engine) as session:
            session.execute(text("SET ROLE public_reader"))
            with pytest.raises(Exception) as exc:
                session.execute(text("SELECT chunk_text FROM knowledge.chunks")).all()
            assert "permission denied" in str(exc.value).lower()
    finally:
        engine.dispose()
