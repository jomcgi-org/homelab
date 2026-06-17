"""Phase 5a' (ADR 004 security/004): real-Postgres confidentiality contract for
the public knowledge views and the endpoints that read them.

Asserts, against a real Postgres (the `pg` fixture applies every migration):
  - public_api.knowledge_notes / knowledge_note_links derive only public,
    non-deleted rows (private and soft-deleted notes never surface),
  - public_reader can SELECT the new edges view but is denied the underlying
    knowledge schema, and
  - GET /api/knowledge/public/* served through the views drops private targets
    and strips private wikilinks, returning an identical 404 for private and
    deleted notes.

Hand-written bdd_test (real DB), so excluded from gazelle. The handler logic
over the view row shape is also covered (SQLite) in knowledge/router_test.py.
"""

import pytest
from sqlmodel import Session, create_engine, text

_INSERT_NOTE = text(
    """
    INSERT INTO knowledge.notes
        (note_id, path, title, content_hash, content, visibility, type, deleted_at)
    VALUES
        (:note_id, :path, :title, :content_hash, :content, :visibility, :type,
         :deleted_at)
    """
)


def _seed(session) -> None:
    # A: public, links in its body to public B and private C.
    session.execute(
        _INSERT_NOTE,
        {
            "note_id": "note-a",
            "path": "note-a.md",
            "title": "A",
            "content_hash": "ha",
            "content": "Body links [[note-b]] and [[note-c]].",
            "visibility": "public",
            "type": "atom",
            "deleted_at": None,
        },
    )
    # B: public.
    session.execute(
        _INSERT_NOTE,
        {
            "note_id": "note-b",
            "path": "note-b.md",
            "title": "B",
            "content_hash": "hb",
            "content": "B body.",
            "visibility": "public",
            "type": "atom",
            "deleted_at": None,
        },
    )
    # C: private (must never surface).
    session.execute(
        _INSERT_NOTE,
        {
            "note_id": "note-c",
            "path": "note-c.md",
            "title": "C",
            "content_hash": "hc",
            "content": "secret body.",
            "visibility": "private",
            "type": "atom",
            "deleted_at": None,
        },
    )
    # D: public but soft-deleted (must never surface).
    session.execute(
        _INSERT_NOTE,
        {
            "note_id": "note-d",
            "path": "note-d.md",
            "title": "D",
            "content_hash": "hd",
            "content": "deleted body.",
            "visibility": "public",
            "type": "atom",
            "deleted_at": "2026-01-01T00:00:00+00:00",
        },
    )
    a_id = session.execute(
        text("SELECT id FROM knowledge.notes WHERE note_id = 'note-a'")
    ).scalar_one()
    session.execute(
        text(
            """
            INSERT INTO knowledge.note_links (src_note_fk, target_id, kind)
            VALUES (:fk, 'note-b', 'link'), (:fk, 'note-c', 'link')
            """
        ),
        {"fk": a_id},
    )
    session.commit()


def test_views_derive_public_only_and_endpoints_filter(session, client):
    """Views expose only public, non-deleted rows; endpoints filter private
    targets and strip private wikilinks. Seeded + read through the SAVEPOINT
    session so nothing persists across tests."""
    _seed(session)

    # --- view derivation (as the migration owner / superuser) ---
    note_ids = [
        r[0]
        for r in session.execute(
            text("SELECT note_id FROM public_api.knowledge_notes ORDER BY note_id")
        ).all()
    ]
    assert note_ids == ["note-a", "note-b"]  # C private, D deleted excluded

    link_rows = session.execute(
        text(
            "SELECT source, target FROM public_api.knowledge_note_links ORDER BY target"
        )
    ).all()
    # Both A-sourced links appear (source is public); target filtering is the
    # handler's job, not the view's.
    assert [(r[0], r[1]) for r in link_rows] == [
        ("note-a", "note-b"),
        ("note-a", "note-c"),
    ]

    # --- public_reader can read the views, sees the same public-only rows ---
    session.execute(text("SET ROLE public_reader"))
    reader_notes = [
        r[0]
        for r in session.execute(
            text("SELECT note_id FROM public_api.knowledge_notes ORDER BY note_id")
        ).all()
    ]
    assert reader_notes == ["note-a", "note-b"]
    reader_links = session.execute(
        text("SELECT source, target FROM public_api.knowledge_note_links")
    ).all()
    assert {(r[0], r[1]) for r in reader_links} == {
        ("note-a", "note-b"),
        ("note-a", "note-c"),
    }
    session.execute(text("RESET ROLE"))

    # --- endpoints over the views ---
    graph = client.get("/api/knowledge/public/graph")
    assert graph.status_code == 200
    g = graph.json()
    assert {n["id"] for n in g["nodes"]} == {"note-a", "note-b"}
    # A->C dropped (target private/absent from the public node set).
    assert {(e["source"], e["target"]) for e in g["edges"]} == {("note-a", "note-b")}

    note_a = client.get("/api/knowledge/public/notes/note-a")
    assert note_a.status_code == 200
    payload = note_a.json()
    body = payload["body"]
    assert "[[note-b]]" in body  # public target kept
    assert "[[note-c]]" not in body  # private target stripped
    assert "note-c" in body  # display text preserved

    # Private and deleted notes return 404, identical to a missing one.
    res_c = client.get("/api/knowledge/public/notes/note-c")
    res_d = client.get("/api/knowledge/public/notes/note-d")
    res_missing = client.get("/api/knowledge/public/notes/does-not-exist")
    assert res_c.status_code == 404
    assert res_d.status_code == 404
    assert res_c.json() == res_missing.json()
    assert res_d.json() == res_missing.json()


def test_public_reader_denied_on_knowledge_note_links(pg):
    """public_reader has SELECT on the edges view but no access to the
    underlying knowledge schema. Mirrors public_reader_grants_test for the
    new view; uses a fresh engine + SET ROLE, no seeded rows needed."""
    engine = create_engine(pg.url)
    try:
        with Session(engine) as session:
            session.execute(text("SET ROLE public_reader"))
            # Granted: the view read must not raise.
            session.execute(
                text("SELECT source, target FROM public_api.knowledge_note_links")
            ).all()
            # Denied: the base table is off-limits.
            with pytest.raises(Exception) as exc:
                session.execute(
                    text("SELECT target_id FROM knowledge.note_links")
                ).all()
            assert "permission denied" in str(exc.value).lower()
    finally:
        engine.dispose()
