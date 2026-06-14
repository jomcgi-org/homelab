"""E2E integration tests for the monolith.

Tests run against real PostgreSQL 16 + pgvector. External services
(Discord, LLMs, SearXNG, vault) are mocked.
"""

import pytest
from pydantic_ai.messages import ToolCallPart
from sqlalchemy import text


def test_postgres_is_running(pg):
    """Smoke test: PostgreSQL is reachable and has pgvector."""
    from sqlmodel import create_engine

    engine = create_engine(pg.url)
    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1")).scalar()
        assert result == 1
        # Verify pgvector extension is loaded
        has_vector = conn.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        ).scalar()
        assert has_vector == 1
    engine.dispose()


# ---------------------------------------------------------------------------
# Task 4: HTTP API E2E Tests
# ---------------------------------------------------------------------------


class TestHealthz:
    def test_healthz_returns_200(self, client):
        """GET /healthz returns 200 OK."""
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestNotesAPI:
    def test_create_note(self, client, tmp_path, monkeypatch):
        """POST returns 201 and writes file to vault."""
        monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
        response = client.post(
            "/api/knowledge/notes",
            json={"content": "Test fleeting note", "title": "Test Note"},
        )
        assert response.status_code == 201
        path = response.json().get("path", "")
        assert path.endswith(".md")

    def test_empty_content_returns_400(self, client, tmp_path, monkeypatch):
        """Empty content returns 400."""
        monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
        response = client.post("/api/knowledge/notes", json={"content": ""})
        assert response.status_code == 400

    def test_whitespace_content_returns_400(self, client, tmp_path, monkeypatch):
        """Whitespace-only content returns 400."""
        monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
        response = client.post("/api/knowledge/notes", json={"content": "   \n\t  "})
        assert response.status_code == 400


class TestScheduleAPI:
    def test_today_returns_list(self, client):
        """Returns list (iCal mocked/empty)."""
        response = client.get("/api/home/schedule/today")
        assert response.status_code == 200
        assert isinstance(response.json(), list)


# ---------------------------------------------------------------------------
# Task 5: MessageStore E2E Tests (pgvector)
# ---------------------------------------------------------------------------


class TestMessageStore:
    @pytest.mark.asyncio
    async def test_save_and_get_recent(self, store):
        """Save message, get_recent returns it with correct fields."""
        msg = await store.save_message(
            discord_message_id="msg-001",
            channel_id="ch-1",
            user_id="u-1",
            username="alice",
            content="Hello from e2e",
            is_bot=False,
        )
        assert msg is not None
        assert msg.content == "Hello from e2e"
        assert msg.username == "alice"

        recent = store.get_recent("ch-1", limit=10)
        assert len(recent) == 1
        assert recent[0].discord_message_id == "msg-001"
        assert recent[0].channel_id == "ch-1"
        assert recent[0].user_id == "u-1"

    @pytest.mark.asyncio
    async def test_duplicate_message_id_returns_none(self, store):
        """IntegrityError on duplicate discord_message_id returns None."""
        await store.save_message(
            discord_message_id="dup-001",
            channel_id="ch-1",
            user_id="u-1",
            username="alice",
            content="First",
            is_bot=False,
        )
        result = await store.save_message(
            discord_message_id="dup-001",
            channel_id="ch-1",
            user_id="u-1",
            username="alice",
            content="Duplicate",
            is_bot=False,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_search_similar_finds_matching_message(self, store, embed_client):
        """Save with embedding, search with same text, pgvector <=> returns it."""
        await store.save_message(
            discord_message_id="sim-001",
            channel_id="ch-search",
            user_id="u-1",
            username="alice",
            content="Kubernetes pod scheduling issues",
            is_bot=False,
        )
        query_embedding = await embed_client.embed("Kubernetes pod scheduling issues")
        results = store.search_similar(
            channel_id="ch-search",
            query_embedding=query_embedding,
            limit=5,
        )
        assert len(results) >= 1
        assert results[0].content == "Kubernetes pod scheduling issues"

    @pytest.mark.asyncio
    async def test_search_similar_filters_by_channel(self, store, embed_client):
        """Messages in other channels excluded."""
        await store.save_message(
            discord_message_id="ch-a-001",
            channel_id="ch-a",
            user_id="u-1",
            username="alice",
            content="Message in channel A",
            is_bot=False,
        )
        await store.save_message(
            discord_message_id="ch-b-001",
            channel_id="ch-b",
            user_id="u-1",
            username="alice",
            content="Message in channel B",
            is_bot=False,
        )
        query_embedding = await embed_client.embed("Message in channel A")
        results = store.search_similar(
            channel_id="ch-a",
            query_embedding=query_embedding,
            limit=10,
        )
        assert all(r.channel_id == "ch-a" for r in results)

    @pytest.mark.asyncio
    async def test_search_similar_filters_by_user(self, store, embed_client):
        """user_id filter works."""
        await store.save_message(
            discord_message_id="user-a-001",
            channel_id="ch-u",
            user_id="u-alice",
            username="alice",
            content="Alice says hello",
            is_bot=False,
        )
        await store.save_message(
            discord_message_id="user-b-001",
            channel_id="ch-u",
            user_id="u-bob",
            username="bob",
            content="Bob says hello",
            is_bot=False,
        )
        query_embedding = await embed_client.embed("hello")
        results = store.search_similar(
            channel_id="ch-u",
            query_embedding=query_embedding,
            limit=10,
            user_id="u-alice",
        )
        assert all(r.user_id == "u-alice" for r in results)
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_attachment_blob_dedup(self, store):
        """Two messages with same image data share one blob row (SHA256 PK)."""
        import hashlib

        from sqlmodel import select

        from chat.models import Blob

        image_data = b"shared-image-bytes"
        attachment = {
            "data": image_data,
            "content_type": "image/png",
            "filename": "pic.png",
            "description": "A picture",
        }
        msg1 = await store.save_message(
            discord_message_id="att-001",
            channel_id="ch-att",
            user_id="u-1",
            username="alice",
            content="First with image",
            is_bot=False,
            attachments=[attachment],
        )
        assert msg1 is not None, (
            "First save_message with attachment returned None (IntegrityError)"
        )
        msg2 = await store.save_message(
            discord_message_id="att-002",
            channel_id="ch-att",
            user_id="u-2",
            username="bob",
            content="Second with same image",
            is_bot=False,
            attachments=[attachment],
        )
        assert msg2 is not None, (
            "Second save_message with attachment returned None (IntegrityError)"
        )
        expected_sha = hashlib.sha256(image_data).hexdigest()
        blobs = store.session.exec(
            select(Blob).where(Blob.sha256 == expected_sha)
        ).all()
        assert len(blobs) == 1

    @pytest.mark.asyncio
    async def test_get_attachments_joins_blobs(self, store):
        """Returns (Attachment, Blob) tuples correctly."""
        attachment = {
            "data": b"blob-data-for-join-test",
            "content_type": "image/jpeg",
            "filename": "photo.jpg",
            "description": "A photo",
        }
        msg = await store.save_message(
            discord_message_id="join-001",
            channel_id="ch-join",
            user_id="u-1",
            username="alice",
            content="Message with attachment",
            is_bot=False,
            attachments=[attachment],
        )
        assert msg is not None
        result = store.get_attachments([msg.id])
        assert msg.id in result
        pairs = result[msg.id]
        assert len(pairs) == 1
        att, blob = pairs[0]
        assert att.filename == "photo.jpg"
        assert blob.content_type == "image/jpeg"

    @pytest.mark.asyncio
    async def test_upsert_summary_insert_and_update(self, store):
        """Insert then update same (channel, user) pair."""
        # Create referenced messages (last_message_id has FK to messages.id)
        msg1 = await store.save_message(
            discord_message_id="sum-msg-001",
            channel_id="ch-sum",
            user_id="u-1",
            username="alice",
            content="Message for summary ref 1",
            is_bot=False,
        )
        msg2 = await store.save_message(
            discord_message_id="sum-msg-002",
            channel_id="ch-sum",
            user_id="u-1",
            username="alice",
            content="Message for summary ref 2",
            is_bot=False,
        )
        store.upsert_summary(
            channel_id="ch-sum",
            user_id="u-1",
            username="alice",
            summary_text="Initial summary",
            last_message_id=msg1.id,
        )
        summary = store.get_user_summary("ch-sum", "alice")
        assert summary is not None
        assert summary.summary == "Initial summary"

        store.upsert_summary(
            channel_id="ch-sum",
            user_id="u-1",
            username="alice",
            summary_text="Updated summary",
            last_message_id=msg2.id,
        )
        summary = store.get_user_summary("ch-sum", "alice")
        assert summary is not None
        assert summary.summary == "Updated summary"
        assert summary.last_message_id == msg2.id

    @pytest.mark.asyncio
    async def test_upsert_summary_unique_constraint(self, store):
        """(channel_id, user_id) enforced, exactly one row."""
        from sqlmodel import select

        from chat.models import UserChannelSummary

        # Create referenced messages (last_message_id has FK to messages.id)
        msg1 = await store.save_message(
            discord_message_id="uniq-msg-001",
            channel_id="ch-uniq",
            user_id="u-1",
            username="alice",
            content="Message for unique constraint ref 1",
            is_bot=False,
        )
        msg2 = await store.save_message(
            discord_message_id="uniq-msg-002",
            channel_id="ch-uniq",
            user_id="u-1",
            username="alice",
            content="Message for unique constraint ref 2",
            is_bot=False,
        )
        store.upsert_summary(
            channel_id="ch-uniq",
            user_id="u-1",
            username="alice",
            summary_text="First",
            last_message_id=msg1.id,
        )
        store.upsert_summary(
            channel_id="ch-uniq",
            user_id="u-1",
            username="alice",
            summary_text="Second",
            last_message_id=msg2.id,
        )
        rows = store.session.exec(
            select(UserChannelSummary).where(
                UserChannelSummary.channel_id == "ch-uniq",
                UserChannelSummary.user_id == "u-1",
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].summary == "Second"


# ---------------------------------------------------------------------------
# Task 6: Agent Tool Execution E2E Tests
# ---------------------------------------------------------------------------


class TestAgentTools:
    @pytest.mark.asyncio
    async def test_search_history_returns_real_pgvector_results(
        self, store, embed_client
    ):
        """Seed a message, run agent with TestModel, verify tool chain completes."""
        from unittest.mock import patch

        from pydantic_ai.models.test import TestModel

        from chat.agent import ChatDeps, create_agent

        await store.save_message(
            discord_message_id="agent-001",
            channel_id="ch-agent",
            user_id="u-1",
            username="alice",
            content="Discussion about deployment strategies",
            is_bot=False,
        )

        agent = create_agent()
        deps = ChatDeps(
            channel_id="ch-agent",
            store=store,
            embed_client=embed_client,
        )
        # Mock web_search since SearXNG is not available in test
        with patch("chat.agent.search_web", return_value="No results found."):
            result = await agent.run(
                "Search for messages about deployment",
                deps=deps,
                model=TestModel(custom_output_text="Found deployment messages."),
            )
        assert result.output == "Found deployment messages."

        # Verify that tools were actually called
        messages = result.all_messages()
        tool_call_messages = [
            m
            for m in messages
            if hasattr(m, "parts") and any(isinstance(p, ToolCallPart) for p in m.parts)
        ]
        assert len(tool_call_messages) >= 1, (
            "Agent should have called at least one tool"
        )

    @pytest.mark.asyncio
    async def test_get_user_summary_returns_real_data(self, store, embed_client):
        """Upsert a summary, run agent with TestModel, verify agent retrieves it."""
        from unittest.mock import patch

        from pydantic_ai.models.test import TestModel

        from chat.agent import ChatDeps, create_agent

        # Create referenced message (last_message_id has FK to messages.id)
        msg = await store.save_message(
            discord_message_id="agent-sum-001",
            channel_id="ch-agent-sum",
            user_id="u-1",
            username="alice",
            content="Message for agent summary ref",
            is_bot=False,
        )
        store.upsert_summary(
            channel_id="ch-agent-sum",
            user_id="u-1",
            username="alice",
            summary_text="Alice has been discussing Kubernetes and GitOps workflows.",
            last_message_id=msg.id,
        )

        agent = create_agent()
        deps = ChatDeps(
            channel_id="ch-agent-sum",
            store=store,
            embed_client=embed_client,
        )
        # Mock web_search since SearXNG is not available in test
        with patch("chat.agent.search_web", return_value="No results found."):
            result = await agent.run(
                "What has alice been talking about?",
                deps=deps,
                model=TestModel(
                    custom_output_text="Alice discusses Kubernetes and GitOps."
                ),
            )
        assert result.output == "Alice discusses Kubernetes and GitOps."

        # Verify that tools were actually called
        messages = result.all_messages()
        tool_call_messages = [
            m
            for m in messages
            if hasattr(m, "parts") and any(isinstance(p, ToolCallPart) for p in m.parts)
        ]
        assert len(tool_call_messages) >= 1, (
            "Agent should have called at least one tool"
        )
