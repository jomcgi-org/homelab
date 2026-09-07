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
    entity_id = session.execute(
        text(
            """
            INSERT INTO knowledge.entities (kind, slug, title, source)
            VALUES ('project', 'view-test-project', 'View Test Project', 'test')
            RETURNING id
            """
        )
    ).scalar_one()
    session.execute(
        text(
            """
            INSERT INTO knowledge.note_entities
                (note_id, entity_id, role, source)
            VALUES
                ('note-a', :entity_id, 'subject', 'test'),
                ('note-c', :entity_id, 'subject', 'test'),
                ('note-d', :entity_id, 'mentions', 'test')
            """
        ),
        {"entity_id": entity_id},
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

    entity_rows = session.execute(
        text("SELECT slug FROM public_api.knowledge_entities")
    ).all()
    assert [row[0] for row in entity_rows] == ["view-test-project"]
    note_entity_rows = session.execute(
        text(
            "SELECT note_id, verification_state FROM public_api.knowledge_note_entities"
        )
    ).all()
    assert [(row[0], row[1]) for row in note_entity_rows] == [("note-a", "legacy")]

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
    assert (
        session.execute(
            text("SELECT count(*) FROM public_api.knowledge_entities")
        ).scalar_one()
        == 1
    )
    assert (
        session.execute(
            text("SELECT count(*) FROM public_api.knowledge_note_entities")
        ).scalar_one()
        == 1
    )
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


def test_public_reader_reads_published_fact_columns_and_sanitized_scope(session):
    """The public role reads publication metadata, but only approved scopes."""
    insert = text(
        """
        INSERT INTO knowledge.notes
            (note_id, path, title, content_hash, content, visibility,
             visibility_verified, type, verification_state, confidence,
             observed_at, scope, valid_from, valid_until, published_at)
        VALUES
            (:note_id, :path, :title, :content_hash, :content, :visibility,
             :visibility_verified, 'fact', :verification_state, :confidence,
             :observed_at, :scope, :valid_from, :valid_until, :published_at)
        """
    )
    common = {
        "content": "Public fact body.",
        "visibility_verified": True,
        "confidence": 0.8,
        "observed_at": "2026-09-07T10:00:00+00:00",
        "scope": "environment:homelab",
        "valid_from": "2026-09-01T00:00:00+00:00",
        "valid_until": None,
        "published_at": "2026-09-07T11:00:00+00:00",
    }
    for note_id, visibility, verification_state, scope in (
        ("published-fact", "public", "verified", "environment:homelab"),
        ("personal-scope-fact", "public", "verified", "personal:joe"),
        ("unpublished-fact", "private", "unverified", "environment:homelab"),
        ("legacy-fact", "private", "legacy", "environment:homelab"),
    ):
        session.execute(
            insert,
            common
            | {
                "note_id": note_id,
                "path": f"{note_id}.md",
                "title": note_id,
                "content_hash": f"hash-{note_id}",
                "visibility": visibility,
                "verification_state": verification_state,
                "scope": scope,
            },
        )
    session.execute(
        text(
            """
            INSERT INTO knowledge.disputes (note_id, reason, state)
            VALUES ('published-fact', 'Needs review', 'open')
            """
        )
    )
    session.commit()

    session.execute(text("SET ROLE public_reader"))
    rows = session.execute(
        text(
            """
            SELECT note_id, verification_state, confidence, observed_at, scope,
                   valid_from, valid_until, published_at, disputed
              FROM public_api.knowledge_notes
             WHERE note_id IN (
                 'published-fact', 'personal-scope-fact',
                 'unpublished-fact', 'legacy-fact'
             )
             ORDER BY note_id
            """
        )
    ).all()
    session.execute(text("RESET ROLE"))
    assert len(rows) == 2
    by_id = {row.note_id: row for row in rows}
    row = by_id["published-fact"]
    assert row.note_id == "published-fact"
    assert row.verification_state == "verified"
    assert row.confidence == pytest.approx(0.8)
    assert row.scope == "environment:homelab"
    assert row.observed_at is not None
    assert row.valid_from is not None
    assert row.valid_until is None
    assert row.published_at is not None
    assert row.disputed is True
    assert by_id["personal-scope-fact"].scope is None


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


def test_scope_shape_constraint_accepts_known_prefix_and_rejects_invalid(session):
    session.execute(
        _INSERT_NOTE,
        {
            "note_id": "valid-scope",
            "path": "valid-scope.md",
            "title": "Valid scope",
            "content_hash": "valid-scope-hash",
            "content": "body",
            "visibility": "private",
            "type": "fact",
            "deleted_at": None,
        },
    )
    session.execute(
        text(
            "UPDATE knowledge.notes SET scope = 'repo:jomcgi-org/homelab' "
            "WHERE note_id = 'valid-scope'"
        )
    )
    session.flush()

    with pytest.raises(Exception) as exc:
        session.execute(
            text(
                "UPDATE knowledge.notes SET scope = 'project:monolith' "
                "WHERE note_id = 'valid-scope'"
            )
        )
        session.flush()
    session.rollback()
    assert "notes_scope_shape_chk" in str(exc.value)
