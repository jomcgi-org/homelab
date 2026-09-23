"""HTTP end-to-end tests for the monolith.

These tests start a real FastAPI server (uvicorn) backed by the test
PostgreSQL instance and drive it over HTTP with ``httpx``, so routing,
serialization, and persistence are exercised against real Postgres rather
than a stub.

There is no browser coverage here. The Playwright suite that used to live
in this file was deleted with issue #4219: it could never run, because
``playwright`` is absent from ``bazel/requirements/all.txt`` and a
module-level ``pytest.importorskip`` turned that into a silent skip of the
whole module. See the issue for the hermetic-Chromium tradeoff.

The first CI run after that guard came off showed the HTTP half had rotted
too: 9 of 15 tests 404'd because they addressed endpoints the monolith no
longer serves. A skipped test cannot fail, so nothing flagged the drift as
the API moved underneath it. What the run found, and what was done:

- ``GET/PUT /api/home``, ``POST /api/home/reset/{daily,weekly}``,
  ``GET /api/home/{dates,weekly}``: a todo/task API that exists nowhere in
  the repo. ``home.register`` mounts exactly three routers (schedule,
  observability, dashboard) and no prefix serves those paths. There is no
  endpoint to repoint these at, so the six tests are deleted and replaced
  with ``TestHomeDashboard``, which covers the ``/api/home`` route that does
  exist.
- ``POST /api/notes``: moved under the knowledge router as
  ``POST /api/knowledge/notes``. Repointed.
- ``GET /api/knowledge/notes/{id}``: the endpoint is fine; the test seeded
  a vault markdown file, which ADR 006 retired. The body of record is the
  Postgres ``notes.content`` column, so ``resolve_note_body(None)`` returned
  ``None`` and the endpoint 404'd. The seed helper now sets ``content``.
"""

import httpx

# ---------------------------------------------------------------------------
# Smoke: live server is reachable (HTTP-only, no browser needed)
# ---------------------------------------------------------------------------


class TestLiveServerSmoke:
    def test_healthz(self, live_server):
        """GET /healthz returns 200 on the live server."""
        r = httpx.get(f"{live_server}/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Home dashboard: the /api/home route the monolith actually serves
# ---------------------------------------------------------------------------


class TestHomeDashboard:
    """Cover ``GET /api/home/dashboard`` (home/dashboard_router.py).

    This replaces the deleted todo/task tests. ``build_dashboard`` gathers its
    three sections concurrently and maps a raised exception to
    ``{"error": ...}`` for that section rather than failing the response, so a
    200 with all three keys present is the contract even here, where the
    ``github`` collector has no credentials to reach the network with.
    """

    def test_dashboard_returns_every_section(self, live_server):
        # One request, not one per assertion: `health` re-scans the cluster on
        # every call when no snapshot row exists, which is the case here.
        r = httpx.get(f"{live_server}/api/home/dashboard", timeout=30.0)
        assert r.status_code == 200
        data = r.json()
        assert set(data) >= {"health", "github", "today", "cached_at"}
        # Each section is a dict: either its payload or the {"error": ...} map.
        for section in ("health", "github", "today"):
            assert isinstance(data[section], dict)


# ---------------------------------------------------------------------------
# Schedule API (iCal feed not configured, returns empty list)
# ---------------------------------------------------------------------------


class TestScheduleAPI:
    def test_today_schedule(self, live_server):
        """GET /api/home/schedule/today returns a list (empty when no iCal feed)."""
        r = httpx.get(f"{live_server}/api/home/schedule/today")
        assert r.status_code == 200
        assert isinstance(r.json(), list)


# ---------------------------------------------------------------------------
# Notes API
# ---------------------------------------------------------------------------


class TestNotesAPI:
    """Cover ``POST /api/knowledge/notes`` (the path ``/api/notes`` moved to).

    Only the request-validation half is exercised over HTTP. A successful
    capture runs ``ingest_raw``, which uploads the body to
    ``s3://knowledge/raws/<raw_id>.md``, and this server has no S3 to reach,
    so a happy-path assertion here could only be satisfied by accepting a 500
    alongside the 201, which is what the old test did, and it would have
    passed against any broken endpoint. Both rejections below are decided
    before the upload, so they are deterministic. ``knowledge/notes_crud_test``
    covers the 201 with ``ingest_raw`` patched.
    """

    def test_empty_note_returns_400(self, live_server):
        """Whitespace-only content is rejected with 400."""
        r = httpx.post(
            f"{live_server}/api/knowledge/notes", json={"content": "   \n  "}
        )
        assert r.status_code == 400
        assert "content" in r.json()["detail"].lower()

    def test_missing_content_returns_422(self, live_server):
        """A body with no ``content`` field fails Pydantic validation with 422."""
        r = httpx.post(f"{live_server}/api/knowledge/notes", json={"title": "No body"})
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Knowledge search HTTP tests (no browser needed)
# ---------------------------------------------------------------------------


def _seed_knowledge_note(
    pg,
    *,
    note_id: str,
    title: str,
    path: str,
    note_type: str = "note",
    tags: list[str] | None = None,
    scope: str | None = None,
    chunk_texts: list[str],
    content: str | None = None,
) -> None:
    """Insert a note + chunks with deterministic embeddings into the test DB.

    Uses a fresh engine+session per call so the data is committed and visible
    to the live server (which uses its own connection pool).

    ``content`` is the note body. Per ADR 006 it is the source of record:
    ``GET /api/knowledge/notes/{id}`` resolves the body from this column and
    404s when it is NULL, so any caller that asserts on the body must pass it.
    It stays optional because the search tests assert on chunk hits and
    frontmatter only, and leaving it NULL there keeps them covering the
    body-less rows the backfill has not reached yet.
    """
    from shared.testing.plugin import deterministic_embedding
    from sqlmodel import Session as SMSession
    from sqlmodel import create_engine as sm_create_engine

    engine = sm_create_engine(pg.url)
    with SMSession(engine) as session:
        from knowledge.models import Chunk, Note

        note = Note(
            note_id=note_id,
            path=path,
            title=title,
            content_hash="e2e-test-hash",
            content=content,
            type=note_type,
            tags=tags or [],
            scope=scope,
        )
        session.add(note)
        session.flush()

        for idx, text in enumerate(chunk_texts):  # nosemgrep: session-add-in-loop
            session.add(
                Chunk(
                    note_fk=note.id,
                    chunk_index=idx,
                    section_header=f"Section {idx}",
                    chunk_text=text,
                    embedding=deterministic_embedding(text),
                )
            )
        session.commit()
    engine.dispose()


def _cleanup_knowledge(pg) -> None:
    """Remove all knowledge rows so tests are isolated."""
    from sqlalchemy import text
    from sqlmodel import create_engine as sm_create_engine

    engine = sm_create_engine(pg.url)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM knowledge.chunks"))
        conn.execute(text("DELETE FROM knowledge.note_links"))
        conn.execute(text("DELETE FROM knowledge.notes"))
    engine.dispose()


class TestKnowledgeSearchHttp:
    """HTTP-level tests for /api/knowledge endpoints (no browser needed).

    These hit the real live FastAPI server + real Postgres, but use a
    deterministic embedding client so no external embedding service is needed.
    """

    def test_knowledge_search_denies_anonymous_caller(
        self, live_server_with_fake_embedding
    ):
        """Anonymous search fails closed before embedding or retrieval."""
        r = httpx.get(f"{live_server_with_fake_embedding}/api/knowledge/search?q=x")
        assert r.status_code == 401
        assert r.json()["detail"]["reason"] == "anonymous"

    def test_knowledge_search_empty_query(self, authorized_knowledge_server):
        """GET /api/knowledge/search?q= returns empty results."""
        base = authorized_knowledge_server
        r = httpx.get(f"{base}/api/knowledge/search?q=")
        assert r.status_code == 200
        assert r.json() == {"results": []}

    def test_knowledge_search_returns_results(self, authorized_knowledge_server, pg):
        """Seed a note, search with matching text, expect it in results."""
        _cleanup_knowledge(pg)
        _seed_knowledge_note(
            pg,
            note_id="e2e-transformers-001",
            title="Transformer Architecture",
            path="notes/transformers.md",
            note_type="note",
            tags=["ml", "architecture"],
            scope="repo:jomcgi-org/homelab",
            chunk_texts=["Transformers use self-attention to process sequences."],
        )

        base = authorized_knowledge_server
        r = httpx.get(
            f"{base}/api/knowledge/search",
            params={"q": "Transformers use self-attention to process sequences."},
        )
        assert r.status_code == 200
        data = r.json()
        assert len(data["results"]) >= 1

        hit = data["results"][0]
        assert hit["note_id"] == "e2e-transformers-001"
        assert hit["title"] == "Transformer Architecture"
        assert hit["type"] == "note"
        assert hit["tags"] == ["ml", "architecture"]
        assert hit["score"] > 0
        assert "snippet" in hit
        assert "section" in hit

        _cleanup_knowledge(pg)

    def test_knowledge_search_type_filter(self, authorized_knowledge_server, pg):
        """Seed two notes with different types, filter by type, expect only matching."""
        _cleanup_knowledge(pg)
        shared_text = "Neural network training and optimization techniques."
        _seed_knowledge_note(
            pg,
            note_id="e2e-filter-article",
            title="Training Neural Nets",
            path="notes/training.md",
            note_type="article",
            scope="repo:jomcgi-org/homelab",
            chunk_texts=[shared_text],
        )
        _seed_knowledge_note(
            pg,
            note_id="e2e-filter-log",
            title="Training Log Entry",
            path="notes/training-log.md",
            note_type="log",
            scope="repo:jomcgi-org/homelab",
            chunk_texts=[shared_text],
        )

        base = authorized_knowledge_server
        r = httpx.get(
            f"{base}/api/knowledge/search",
            params={"q": shared_text, "type": "article"},
        )
        assert r.status_code == 200
        data = r.json()
        results = data["results"]
        assert len(results) >= 1
        assert all(hit["type"] == "article" for hit in results)
        assert any(hit["note_id"] == "e2e-filter-article" for hit in results)
        assert not any(hit["note_id"] == "e2e-filter-log" for hit in results)

        _cleanup_knowledge(pg)

    def test_knowledge_note_returns_content(self, live_server_with_fake_embedding, pg):
        """Seed a note body, GET it by id, expect that body back.

        The body lives in ``knowledge.notes.content`` (ADR 006). This test
        used to write a markdown file and point ``VAULT_ROOT`` at it, which
        stopped meaning anything when Obsidian was decommissioned and
        ``resolve_note_body`` became an identity over the column.
        """
        _cleanup_knowledge(pg)

        body = "# E2E Content\n\nThis is the note body of record."
        try:
            _seed_knowledge_note(
                pg,
                note_id="e2e-content-001",
                title="E2E Content Note",
                path="notes/e2e-content.md",
                chunk_texts=["E2E content for testing."],
                content=body,
            )

            base = live_server_with_fake_embedding
            r = httpx.get(f"{base}/api/knowledge/notes/e2e-content-001")
            assert r.status_code == 200
            data = r.json()
            assert data["note_id"] == "e2e-content-001"
            assert data["title"] == "E2E Content Note"
            assert data["content"] == body
            assert data["edges"] == []
        finally:
            _cleanup_knowledge(pg)

    def test_knowledge_note_without_body_returns_404(
        self, live_server_with_fake_embedding, pg
    ):
        """A row whose ``content`` is NULL has no body to serve, so 404.

        This is the exact shape that made the pre-#4219 version of
        ``test_knowledge_note_returns_content`` fail once it was allowed to
        run, so it is pinned rather than left implicit.
        """
        _cleanup_knowledge(pg)
        try:
            _seed_knowledge_note(
                pg,
                note_id="e2e-bodyless-001",
                title="Body-less Note",
                path="notes/e2e-bodyless.md",
                chunk_texts=["Indexed but not backfilled."],
            )

            base = live_server_with_fake_embedding
            r = httpx.get(f"{base}/api/knowledge/notes/e2e-bodyless-001")
            assert r.status_code == 404
            assert r.json()["detail"] == "note has no body"
        finally:
            _cleanup_knowledge(pg)

    def test_knowledge_note_missing_returns_404(self, live_server_with_fake_embedding):
        """GET /api/knowledge/notes/nonexistent returns 404."""
        base = live_server_with_fake_embedding
        r = httpx.get(f"{base}/api/knowledge/notes/nonexistent")
        assert r.status_code == 404
        assert r.json()["detail"] == "note not found"
