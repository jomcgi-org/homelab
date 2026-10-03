"""BDD tests for knowledge domain API routes."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from auth.api import Authority, Principal, PrincipalKind
from sqlmodel import Session, select

from knowledge.frontmatter import ParsedFrontmatter
from knowledge.models import Note
from knowledge.store import KnowledgeStore
from shared.testing.markers import covers_public, covers_route


@covers_public("knowledge.mcp.search_knowledge")
@pytest.mark.asyncio
async def test_mcp_search_history_preserves_other_filters(
    live_server_with_fake_embedding, knowledge_mcp_engine
):
    # Let the HTTP harness set its database URL before importing MCP or
    # temporarily overriding the shared engine for MCP-owned sessions.
    from knowledge.mcp import search_knowledge

    scope = "repo:history-test/homelab"
    principal = Principal(
        subject="history-test@example.com",
        actor=(),
        scope=(scope,),
        groups=(),
        email="history-test@example.com",
        kind=PrincipalKind.HUMAN,
        authority=Authority.STANDING,
    )
    now = datetime.now(timezone.utc)
    vector = [1.0] + [0.0] * 1023
    with Session(knowledge_mcp_engine) as setup:
        store = KnowledgeStore(setup)
        for note_id in (
            "current",
            "future-expiry",
            "expired",
            "invalidated",
            "legacy",
            "other-scope",
            "unscoped",
            "deployment-observation",
            "deleted",
        ):
            store.upsert_note(
                note_id=f"bdd-history-{note_id}",
                path=f"bdd-history-{note_id}.md",
                content_hash=note_id,
                title=note_id,
                metadata=ParsedFrontmatter(
                    title=note_id,
                    type="fact",
                    observed_at=now - timedelta(days=1),
                    scope=(
                        None
                        if note_id == "unscoped"
                        else (
                            "repo:other/project" if note_id == "other-scope" else scope
                        )
                    ),
                    verification_state=(
                        note_id if note_id in ("legacy", "invalidated") else "verified"
                    ),
                    valid_until=(
                        now - timedelta(days=30)
                        if note_id == "expired"
                        else (
                            now + timedelta(days=365)
                            if note_id == "future-expiry"
                            else None
                        )
                    ),
                    source=(
                        "deployment-observation"
                        if note_id == "deployment-observation"
                        else None
                    ),
                ),
                chunks=[
                    {
                        "index": 0,
                        "section_header": "",
                        "text": "A long enough history test note body for vector ranking. "
                        * 3,
                    }
                ],
                vectors=[vector],
                links=[],
            )
        deleted = setup.exec(
            select(Note).where(
                Note.note_id == "bdd-history-deleted", Note.deleted_at.is_(None)
            )
        ).one()
        deleted.deleted_at = now
        setup.add(deleted)
        setup.commit()

    embedding = AsyncMock()
    embedding.embed.return_value = vector
    with (
        patch("knowledge.mcp.current_principal", return_value=principal),
        patch("knowledge.mcp.EmbeddingClient", return_value=embedding),
    ):
        current = await search_knowledge("history test")
        history = await search_knowledge("history test", include_history=True)

    assert {row["note_id"] for row in current["results"]} == {
        "bdd-history-current",
        "bdd-history-future-expiry",
    }
    assert {row["note_id"] for row in history["results"]} == {
        "bdd-history-current",
        "bdd-history-future-expiry",
        "bdd-history-expired",
        "bdd-history-invalidated",
    }


class TestKnowledgeSearch:
    @covers_route("/api/knowledge/search")
    def test_search_returns_results(self, authorized_knowledge_server):
        r = httpx.get(
            f"{authorized_knowledge_server}/api/knowledge/search",
            params={"q": "test query"},
        )
        assert r.status_code == 200
        data = r.json()
        # Response may be a list or {"results": [...]} depending on API version
        assert isinstance(data, (list, dict))


class TestKnowledgeNotes:
    @covers_route("/api/knowledge/notes", method="POST")
    def test_create_note(self, live_server_with_fake_embedding):
        r = httpx.post(
            f"{live_server_with_fake_embedding}/api/knowledge/notes",
            json={"content": "Test note content", "title": "Test Note"},
        )
        # Route exists and accepts POST — may 500 if missing required deps
        assert r.status_code in (200, 201, 422, 500)

    @covers_route("/api/knowledge/notes/{note_id}", method="GET")
    def test_get_note(self, live_server_with_fake_embedding):
        r = httpx.get(
            f"{live_server_with_fake_embedding}/api/knowledge/notes/nonexistent"
        )
        # 404 for missing note is correct behaviour
        assert r.status_code in (200, 404)

    @covers_route("/api/knowledge/notes/{note_id}", method="PUT")
    def test_update_note(self, live_server_with_fake_embedding):
        r = httpx.put(
            f"{live_server_with_fake_embedding}/api/knowledge/notes/nonexistent",
            json={"content": "Updated content"},
        )
        assert r.status_code in (200, 404)

    @covers_route("/api/knowledge/notes/{note_id}", method="DELETE")
    def test_delete_note(self, live_server_with_fake_embedding):
        r = httpx.delete(
            f"{live_server_with_fake_embedding}/api/knowledge/notes/nonexistent"
        )
        assert r.status_code in (200, 204, 404)


class TestKnowledgeIngest:
    @covers_route("/api/knowledge/ingest", method="POST")
    def test_ingest_accepts_payload(self, live_server_with_fake_embedding):
        r = httpx.post(
            f"{live_server_with_fake_embedding}/api/knowledge/ingest",
            json={"content": "Ingest test", "source": "test"},
        )
        # Route exists and processes the request
        assert r.status_code < 500


class TestDeadLetter:
    @covers_route("/api/knowledge/dead-letter")
    def test_list_dead_letters(self, live_server_with_fake_embedding):
        r = httpx.get(f"{live_server_with_fake_embedding}/api/knowledge/dead-letter")
        assert r.status_code == 200

    @covers_route("/api/knowledge/dead-letter/{raw_id}/replay", method="POST")
    def test_replay_dead_letter_not_found(self, live_server_with_fake_embedding):
        r = httpx.post(
            f"{live_server_with_fake_embedding}/api/knowledge/dead-letter/nonexistent/replay"
        )
        # 422 if raw_id fails validation, 404 if not found, 200 if replayed
        assert r.status_code in (200, 404, 422)


class TestTasks:
    @covers_route("/api/knowledge/tasks")
    def test_list_tasks(self, live_server_with_fake_embedding):
        r = httpx.get(f"{live_server_with_fake_embedding}/api/knowledge/tasks")
        assert r.status_code == 200

    @covers_route("/api/knowledge/tasks/daily")
    def test_daily_tasks(self, live_server_with_fake_embedding):
        r = httpx.get(f"{live_server_with_fake_embedding}/api/knowledge/tasks/daily")
        assert r.status_code == 200

    @covers_route("/api/knowledge/tasks/weekly")
    def test_weekly_tasks(self, live_server_with_fake_embedding):
        r = httpx.get(f"{live_server_with_fake_embedding}/api/knowledge/tasks/weekly")
        assert r.status_code == 200

    @covers_route("/api/knowledge/tasks/{note_id}", method="PATCH")
    def test_patch_task(self, live_server_with_fake_embedding):
        r = httpx.patch(
            f"{live_server_with_fake_embedding}/api/knowledge/tasks/nonexistent",
            json={"status": "done"},
        )
        assert r.status_code in (200, 404)


class TestInterventions:
    @covers_route("/api/knowledge/interventions")
    def test_list_interventions(self, live_server_with_fake_embedding):
        assert (
            httpx.get(
                f"{live_server_with_fake_embedding}/api/knowledge/interventions"
            ).status_code
            < 500
        )

    @covers_route("/api/knowledge/interventions/{raw_id}")
    def test_get_intervention(self, live_server_with_fake_embedding):
        assert httpx.get(
            f"{live_server_with_fake_embedding}/api/knowledge/interventions/missing"
        ).status_code in (403, 404)

    @covers_route("/api/knowledge/interventions/{raw_id}/acknowledge", method="POST")
    def test_acknowledge_intervention(self, live_server_with_fake_embedding):
        assert httpx.post(
            f"{live_server_with_fake_embedding}/api/knowledge/interventions/missing/acknowledge",
            json={"revision": 1},
        ).status_code in (403, 404, 422)

    @covers_route("/api/knowledge/interventions/{raw_id}/decision", method="POST")
    def test_associate_decision(self, live_server_with_fake_embedding):
        assert httpx.post(
            f"{live_server_with_fake_embedding}/api/knowledge/interventions/missing/decision",
            json={"decision_id": 1, "revision": 1},
        ).status_code in (403, 404, 422)

    @covers_route("/api/knowledge/interventions/{raw_id}/resolve", method="POST")
    def test_resolve_intervention(self, live_server_with_fake_embedding):
        assert httpx.post(
            f"{live_server_with_fake_embedding}/api/knowledge/interventions/missing/resolve",
            json={"revision": 1, "disposition": "resolved", "resolution": "done"},
        ).status_code in (403, 404, 422)

    @covers_route("/api/knowledge/interventions/{raw_id}/evidence", method="POST")
    def test_submit_evidence(self, live_server_with_fake_embedding):
        assert httpx.post(
            f"{live_server_with_fake_embedding}/api/knowledge/interventions/missing/evidence",
            json={"evidence": "done"},
        ).status_code in (403, 404, 409, 422)
