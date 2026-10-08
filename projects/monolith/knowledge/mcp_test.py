"""Unit tests for knowledge/mcp.py MCP search, notes, and task tools."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from auth.api import Authority, Principal, PrincipalKind, anonymous_principal
from sqlalchemy.dialects import postgresql
from sqlmodel import Session, select

from knowledge.mcp import (
    get_daily_tasks,
    get_note,
    get_weekly_tasks,
    grant_kg_burst,
    list_tasks,
    search_knowledge,
    search_tasks,
    update_task,
)
from knowledge.models import Note

FAKE_EMBEDDING = [0.1] * 1024
DEFAULT_SCOPES = (
    "org:jomcgi-org",
    "repo:jomcgi-org/homelab",
    "environment:homelab",
)


def _principal(
    *,
    personal: bool = False,
    scopes: tuple[str, ...] | None = None,
    groups: tuple[str, ...] = ("operators",),
) -> Principal:
    subject = "agent@example.com"
    grants = DEFAULT_SCOPES if scopes is None else scopes
    if personal:
        grants = (*grants, f"personal:{subject}")
    return Principal(
        subject=subject,
        actor=(),
        scope=grants,
        groups=groups,
        email=subject,
        kind=PrincipalKind.HUMAN,
        authority=Authority.STANDING,
    )


CANNED_TASKS = [
    {
        "note_id": "t1",
        "title": "Fix auth bug",
        "tags": ["backend"],
        "status": "todo",
        "due": "2026-04-20",
        "size": "small",
        "blocked_by": [],
        "task_completed": None,
    },
]

CANNED_RESULTS = [
    {
        "note_id": "n1",
        "title": "Attention Is All You Need",
        "path": "papers/attention.md",
        "type": "paper",
        "tags": ["ml", "transformers"],
        "score": 0.95,
        "section": "## Architecture",
        "snippet": "The transformer replaces recurrence entirely with attention.",
        "edges": [],
    },
]

SAMPLE_NOTE = {
    "note_id": "n1",
    "title": "Attention Is All You Need",
    "path": "papers/attention.md",
    "type": "paper",
    "tags": ["ml", "transformers"],
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra_jobs", "duration_minutes", "message"),
    [
        (1_001, 60, "extra_jobs must not exceed 1000"),
        (100, 1_441, "duration_seconds must not exceed 86400 (24 hours)"),
    ],
)
async def test_grant_kg_burst_rejects_unsafe_limits(
    extra_jobs, duration_minutes, message
):
    principal = MagicMock()
    principal.has_group.return_value = True
    with (
        patch("knowledge.mcp.current_principal", return_value=principal),
        patch("knowledge.mcp._grant_kg_burst_sync") as grant_sync,
    ):
        result = await grant_kg_burst(extra_jobs, duration_minutes)

    assert result == {"error": message}
    grant_sync.assert_not_called()


@pytest.mark.asyncio
async def test_grant_kg_burst_returns_created_grant():
    principal = MagicMock()
    principal.has_group.return_value = True
    principal.authority = "standing"
    principal.subject = "operator@example.com"
    granted = {
        "grant_id": 7,
        "extra_jobs": 500,
        "duration_seconds": 7_200,
        "created_at": "2026-09-04T12:00:00+00:00",
        "expires_at": "2026-09-04T14:00:00+00:00",
        "created_by": "standing:operator@example.com",
    }
    with (
        patch("knowledge.mcp.current_principal", return_value=principal),
        patch("knowledge.mcp._grant_kg_burst_sync", return_value=granted) as grant_sync,
    ):
        result = await grant_kg_burst(500, 120)

    assert result == granted
    grant_sync.assert_called_once_with(500, 7_200, "standing:operator@example.com")


def _get_note_row(engine, note_id: str) -> Note:
    """Fetch the raw Note ORM row (carries columns get_note_by_id omits)."""
    with Session(engine) as session:
        # test helper: intentionally reads any row (including soft-deleted).
        stmt = select(Note).where(
            Note.note_id == note_id
        )  # nosemgrep: sqlmodel-select-missing-deleted-at-filter
        return session.exec(stmt).one()


class TestSearchKnowledge:
    """Tests for the search_knowledge MCP tool."""

    @pytest.fixture(autouse=True)
    def authorized_principal(self):
        with patch("knowledge.mcp.current_principal", return_value=_principal()):
            yield

    @pytest.mark.asyncio
    async def test_returns_results(self):
        mock_session = MagicMock()
        mock_embed = AsyncMock()
        mock_embed.embed.return_value = FAKE_EMBEDDING

        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=mock_embed),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.search_notes_with_context.return_value = (
                CANNED_RESULTS
            )
            result = await search_knowledge("attention")

        assert len(result["results"]) == 1
        assert result["results"][0]["note_id"] == "n1"
        mock_embed.embed.assert_awaited_once_with("attention")
        MockStore.return_value.search_notes_with_context.assert_called_once_with(
            query_embedding=FAKE_EMBEDDING,
            query_text="attention",
            limit=20,
            type_filter=None,
            scope_filters=DEFAULT_SCOPES,
            include_unscoped=False,
            include_deployment_observations=False,
            exclude_invalidated=True,
            include_history=False,
        )

    @pytest.mark.asyncio
    async def test_deployment_observations_opt_in_is_forwarded(self):
        mock_embed = AsyncMock()
        mock_embed.embed.return_value = FAKE_EMBEDDING

        with (
            patch("knowledge.mcp.Session", return_value=MagicMock()),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=mock_embed),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.search_notes_with_context.return_value = []
            await search_knowledge("attention", include_deployment_observations=True)

        MockStore.return_value.search_notes_with_context.assert_called_once_with(
            query_embedding=FAKE_EMBEDDING,
            query_text="attention",
            limit=20,
            type_filter=None,
            scope_filters=DEFAULT_SCOPES,
            include_unscoped=False,
            include_deployment_observations=True,
            exclude_invalidated=True,
            include_history=False,
        )

    @pytest.mark.asyncio
    async def test_raw_exact_query_is_forwarded(self):
        query = "Status of #12 at `projects/a_b/store.py`?"
        mock_embed = AsyncMock()
        mock_embed.embed.return_value = FAKE_EMBEDDING
        with (
            patch("knowledge.mcp.Session", return_value=MagicMock()),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=mock_embed),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.search_notes_with_context.return_value = []
            await search_knowledge(query)
        mock_embed.embed.assert_awaited_once_with(query)
        assert (
            MockStore.return_value.search_notes_with_context.call_args.kwargs[
                "query_text"
            ]
            == query
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("include_personal", [False, True])
    @pytest.mark.parametrize("include_deployment_observations", [False, True])
    @pytest.mark.parametrize("scope", [None, "repo:jomcgi-org/homelab"])
    async def test_history_opt_in_preserves_other_filters(
        self, include_personal, include_deployment_observations, scope
    ):
        mock_embed = AsyncMock()
        mock_embed.embed.return_value = FAKE_EMBEDDING
        with (
            patch(
                "knowledge.mcp.current_principal",
                return_value=_principal(personal=include_personal),
            ),
            patch("knowledge.mcp.Session", return_value=MagicMock()),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=mock_embed),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.search_notes_with_context.return_value = []
            await search_knowledge(
                "attention",
                include_history=True,
                include_personal=include_personal,
                include_deployment_observations=include_deployment_observations,
                scope=scope,
            )

        if scope is None:
            MockStore.return_value.search_notes_with_context.assert_called_once_with(
                query_embedding=FAKE_EMBEDDING,
                query_text="attention",
                limit=20,
                type_filter=None,
                scope_filters=(
                    (*DEFAULT_SCOPES, "personal:agent@example.com")
                    if include_personal
                    else DEFAULT_SCOPES
                ),
                include_unscoped=include_personal,
                include_deployment_observations=include_deployment_observations,
                exclude_invalidated=False,
                include_history=True,
            )
        else:
            MockStore.return_value.search_notes_with_context.assert_called_once_with(
                query_embedding=FAKE_EMBEDDING,
                query_text="attention",
                limit=20,
                type_filter=None,
                scope_filter=scope,
                include_unscoped=False,
                include_deployment_observations=include_deployment_observations,
                exclude_invalidated=False,
                include_history=True,
            )

    @pytest.mark.asyncio
    async def test_short_query_returns_empty(self):
        result = await search_knowledge("a")
        assert result == {"results": []}

    @pytest.mark.asyncio
    async def test_empty_query_returns_empty(self):
        result = await search_knowledge("")
        assert result == {"results": []}

    @pytest.mark.asyncio
    async def test_limit_and_type_forwarded(self):
        mock_session = MagicMock()
        mock_embed = AsyncMock()
        mock_embed.embed.return_value = FAKE_EMBEDDING

        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=mock_embed),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.search_notes_with_context.return_value = []
            await search_knowledge("attention", limit=5, type="paper")

            MockStore.return_value.search_notes_with_context.assert_called_once_with(
                query_embedding=FAKE_EMBEDDING,
                query_text="attention",
                limit=5,
                type_filter="paper",
                scope_filters=DEFAULT_SCOPES,
                include_unscoped=False,
                include_deployment_observations=False,
                exclude_invalidated=True,
                include_history=False,
            )

    @pytest.mark.asyncio
    async def test_embedding_failure_returns_error(self):
        mock_embed = AsyncMock()
        mock_embed.embed.side_effect = RuntimeError("boom")

        with (
            patch("knowledge.mcp.Session"),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=mock_embed),
        ):
            result = await search_knowledge("hello")

        assert "error" in result
        assert result["reason"] == "embedding_failed"

    @pytest.mark.asyncio
    async def test_anonymous_and_unmapped_principals_have_distinct_denials(self):
        mock_embed = AsyncMock()
        with (
            patch("knowledge.mcp.EmbeddingClient", return_value=mock_embed),
            patch(
                "knowledge.mcp.current_principal",
                return_value=anonymous_principal(),
            ),
        ):
            anonymous = await search_knowledge("attention")
        with patch(
            "knowledge.mcp.current_principal",
            return_value=_principal(scopes=("openid", "profile")),
        ):
            unmapped = await search_knowledge("attention")
        with patch(
            "knowledge.mcp.current_principal",
            return_value=_principal(scopes=(), groups=()),
        ):
            empty_grants = await search_knowledge("attention")

        assert anonymous["reason"] == "anonymous"
        assert unmapped["reason"] == "unmapped_principal"
        assert empty_grants["reason"] == "unmapped_principal"
        mock_embed.embed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cross_subject_personal_opt_in_is_denied_before_audit(self):
        mock_embed = AsyncMock()
        with (
            patch(
                "knowledge.mcp.current_principal",
                return_value=_principal(
                    scopes=(*DEFAULT_SCOPES, "personal:someone-else")
                ),
            ),
            patch("knowledge.mcp.audit_personal_retrieval") as audit,
        ):
            result = await search_knowledge("attention", include_personal=True)

        assert result["reason"] == "personal_scope_not_granted"
        audit.assert_not_called()
        mock_embed.embed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_verified_homelab_group_maps_to_exact_repository_scopes(self):
        mock_embed = AsyncMock()
        mock_embed.embed.return_value = FAKE_EMBEDDING
        with (
            patch(
                "knowledge.mcp.current_principal",
                return_value=_principal(
                    scopes=("openid", "profile"),
                    groups=("homelab-admin", "operators"),
                ),
            ),
            patch("knowledge.mcp.Session"),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=mock_embed),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.search_notes_with_context.return_value = []
            result = await search_knowledge("attention")

        assert result == {"results": []}
        MockStore.return_value.search_notes_with_context.assert_called_once_with(
            query_embedding=FAKE_EMBEDDING,
            query_text="attention",
            limit=20,
            type_filter=None,
            scope_filters=DEFAULT_SCOPES,
            include_unscoped=False,
            include_deployment_observations=False,
            exclude_invalidated=True,
            include_history=False,
        )

    @pytest.mark.asyncio
    async def test_personal_opt_in_is_audited_once_and_filters_personal_and_null(self):
        session = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = session
        mock_embed = AsyncMock()
        mock_embed.embed.return_value = FAKE_EMBEDDING
        principal = _principal(personal=True)
        with (
            patch("knowledge.mcp.current_principal", return_value=principal),
            patch("knowledge.mcp.Session", return_value=context),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=mock_embed),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
            patch("knowledge.mcp.audit_personal_retrieval") as audit,
        ):
            MockStore.return_value.search_notes_with_context.return_value = []
            result = await search_knowledge("attention", include_personal=True)

        assert result == {"results": []}
        audit.assert_called_once()
        MockStore.return_value.search_notes_with_context.assert_called_once_with(
            query_embedding=FAKE_EMBEDDING,
            query_text="attention",
            limit=20,
            type_filter=None,
            scope_filters=(*DEFAULT_SCOPES, "personal:agent@example.com"),
            include_unscoped=True,
            include_deployment_observations=False,
            exclude_invalidated=True,
            include_history=False,
        )

    @pytest.mark.asyncio
    async def test_personal_audit_failure_denies_before_embedding(self):
        from knowledge.retrieval_policy import RetrievalAuditError

        mock_embed = AsyncMock()
        with (
            patch(
                "knowledge.mcp.current_principal",
                return_value=_principal(personal=True),
            ),
            patch("knowledge.mcp.Session"),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=mock_embed),
            patch(
                "knowledge.mcp.audit_personal_retrieval",
                side_effect=RetrievalAuditError("no audit"),
            ),
        ):
            result = await search_knowledge("attention", include_personal=True)

        assert result["reason"] == "audit_unavailable"
        mock_embed.embed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_personal_embedding_failure_occurs_after_one_audit(self):
        mock_embed = AsyncMock()
        mock_embed.embed.side_effect = RuntimeError("boom")
        with (
            patch(
                "knowledge.mcp.current_principal",
                return_value=_principal(personal=True),
            ),
            patch("knowledge.mcp.Session"),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=mock_embed),
            patch("knowledge.mcp.audit_personal_retrieval") as audit,
        ):
            result = await search_knowledge("attention", include_personal=True)

        assert result["reason"] == "embedding_failed"
        audit.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "principal",
        [
            _principal(),
            _principal(scopes=("openid",), groups=("homelab-admin",)),
            _principal(scopes=(), groups=("kg-agents",)),
        ],
    )
    async def test_scope_narrows_exact_and_group_grants(self, principal):
        mock_embed = AsyncMock()
        mock_embed.embed.return_value = FAKE_EMBEDDING
        with (
            patch("knowledge.mcp.current_principal", return_value=principal),
            patch("knowledge.mcp.Session"),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=mock_embed),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.search_notes_with_context.return_value = (
                CANNED_RESULTS
            )
            result = await search_knowledge(
                "attention",
                limit=5,
                type="paper",
                include_deployment_observations=True,
                scope="repo:jomcgi-org/homelab",
            )

        assert result == {"results": CANNED_RESULTS}
        mock_embed.embed.assert_awaited_once_with("attention")
        MockStore.return_value.search_notes_with_context.assert_called_once_with(
            query_embedding=FAKE_EMBEDDING,
            query_text="attention",
            limit=5,
            type_filter="paper",
            scope_filter="repo:jomcgi-org/homelab",
            include_unscoped=False,
            include_deployment_observations=True,
            exclude_invalidated=True,
            include_history=False,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "scope", ["personal:agent@example.com", "repo:jomcgi-org/homelab"]
    )
    async def test_scope_with_personal_opt_in_is_audited_and_excludes_null(self, scope):
        session = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = session
        mock_embed = AsyncMock()
        mock_embed.embed.return_value = FAKE_EMBEDDING
        principal = _principal(personal=True)
        with (
            patch("knowledge.mcp.current_principal", return_value=principal),
            patch("knowledge.mcp.Session", return_value=context),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=mock_embed),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
            patch("knowledge.mcp.audit_personal_retrieval") as audit,
        ):
            MockStore.return_value.search_notes_with_context.return_value = []
            result = await search_knowledge(
                "attention", include_personal=True, scope=scope
            )

        assert result == {"results": []}
        audit.assert_called_once_with(
            session, principal, audit.call_args.args[2], entrypoint="mcp"
        )
        assert audit.call_args.args[2].personal_scope == "personal:agent@example.com"
        MockStore.return_value.search_notes_with_context.assert_called_once_with(
            query_embedding=FAKE_EMBEDDING,
            query_text="attention",
            limit=20,
            type_filter=None,
            scope_filter=scope,
            include_unscoped=False,
            include_deployment_observations=False,
            exclude_invalidated=True,
            include_history=False,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "scope",
        [
            "repo:other-org/other",
            "repo:",
            "repo:jomcgi-org",
            "repo:jomcgi-org/*",
            "REPO:jomcgi-org/homelab",
            "repo:jomcgi-org/homelab ",
            "",
            "personal:agent@example.com",
        ],
    )
    async def test_scope_widening_and_near_misses_return_only_empty_results(
        self, scope
    ):
        mock_embed = AsyncMock()
        with (
            patch("knowledge.mcp.Session"),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=mock_embed) as Embed,
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            result = await search_knowledge("attention", scope=scope)

        assert result == {"results": []}
        Embed.assert_not_called()
        mock_embed.embed.assert_not_awaited()
        MockStore.assert_not_called()

    @pytest.mark.asyncio
    async def test_refused_scope_is_audited_before_return_when_personal_is_requested(
        self,
    ):
        mock_embed = AsyncMock()
        with (
            patch(
                "knowledge.mcp.current_principal",
                return_value=_principal(personal=True),
            ),
            patch("knowledge.mcp.Session"),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=mock_embed),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
            patch("knowledge.mcp.audit_personal_retrieval") as audit,
        ):
            result = await search_knowledge(
                "attention", include_personal=True, scope="repo:other-org/other"
            )

        assert result == {"results": []}
        audit.assert_called_once()
        mock_embed.embed.assert_not_awaited()
        MockStore.assert_not_called()

    @pytest.mark.asyncio
    async def test_refused_scope_and_authorized_empty_search_have_identical_shape(self):
        mock_embed = AsyncMock()
        mock_embed.embed.return_value = FAKE_EMBEDDING
        with (
            patch("knowledge.mcp.Session"),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=mock_embed),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.search_notes_with_context.return_value = []
            empty = await search_knowledge("attention", scope="repo:jomcgi-org/homelab")
            refused = await search_knowledge("attention", scope="repo:other-org/other")

        assert refused == empty == {"results": []}
        mock_embed.embed.assert_awaited_once_with("attention")
        MockStore.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("scope", [None, "repo:jomcgi-org/homelab"])
    async def test_scope_reaches_real_store_ranking_sql(self, scope):
        session = MagicMock()
        session.execute.return_value.all.return_value = []
        context = MagicMock()
        context.__enter__.return_value = session
        mock_embed = AsyncMock()
        mock_embed.embed.return_value = FAKE_EMBEDDING
        with (
            patch("knowledge.mcp.Session", return_value=context),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=mock_embed),
        ):
            kwargs = {} if scope is None else {"scope": scope}
            result = await search_knowledge("attention", **kwargs)

        assert result == {"results": []}
        session.execute.assert_called_once()
        statement = session.execute.call_args.args[0]
        compiled = statement.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        assert "knowledge.notes.scope IN" in sql
        assert "knowledge.notes.scope IS NULL" not in sql
        assert sql.index("WHERE") < sql.index("LIMIT")
        expected_scopes = list(DEFAULT_SCOPES) if scope is None else [scope]
        assert compiled.params["scope_1"] == expected_scopes


class TestGetNote:
    """Tests for the get_note MCP tool."""

    @pytest.mark.asyncio
    async def test_returns_note_with_content(self):
        """ADR 006: body comes from the authoritative Postgres ``content``."""
        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.get_note_by_id.return_value = {
                **SAMPLE_NOTE,
                "content": "# Attention\n\nSelf-attention mechanism.",
            }
            MockStore.return_value.get_note_links.return_value = []
            result = await get_note("n1")

        assert result["note_id"] == "n1"
        assert result["content"] == "# Attention\n\nSelf-attention mechanism."
        assert result["edges"] == []

    @pytest.mark.asyncio
    async def test_missing_note_returns_error(self):
        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.get_note_by_id.return_value = None
            result = await get_note("nonexistent")

        assert "error" in result

    @pytest.mark.asyncio
    async def test_missing_body_returns_error(self):
        """ADR 006: a row with a NULL ``content`` has no body to serve."""
        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.get_note_by_id.return_value = {
                **SAMPLE_NOTE,
                "content": None,
            }
            result = await get_note("n1")

        assert "error" in result
        assert "no body" in result["error"]


class TestListTasks:
    """Tests for the list_tasks MCP tool."""

    @pytest.mark.asyncio
    async def test_returns_tasks(self):
        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.list_tasks.return_value = CANNED_TASKS
            result = await list_tasks()

        assert len(result["tasks"]) == 1
        assert result["tasks"][0]["note_id"] == "t1"
        MockStore.return_value.list_tasks.assert_called_once_with(
            statuses=None,
            due_before=None,
            due_after=None,
            sizes=None,
            include_someday=False,
        )

    @pytest.mark.asyncio
    async def test_forwards_filters(self):
        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.list_tasks.return_value = []
            await list_tasks(
                status="todo,in-progress",
                due_before="2026-04-25",
                due_after="2026-04-18",
                size="small,medium",
                include_someday=True,
            )

            MockStore.return_value.list_tasks.assert_called_once_with(
                statuses=["todo", "in-progress"],
                due_before="2026-04-25",
                due_after="2026-04-18",
                sizes=["small", "medium"],
                include_someday=True,
            )


class TestSearchTasks:
    """Tests for the search_tasks MCP tool."""

    @pytest.mark.asyncio
    async def test_returns_results(self):
        mock_session = MagicMock()
        mock_embed = AsyncMock()
        mock_embed.embed.return_value = FAKE_EMBEDDING

        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=mock_embed),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.search_tasks.return_value = CANNED_TASKS
            result = await search_tasks("fix auth")

        assert len(result["tasks"]) == 1
        assert result["tasks"][0]["note_id"] == "t1"

    @pytest.mark.asyncio
    async def test_short_query_returns_empty(self):
        result = await search_tasks("a")
        assert result == {"tasks": []}

    @pytest.mark.asyncio
    async def test_forwards_filters(self):
        mock_session = MagicMock()
        mock_embed = AsyncMock()
        mock_embed.embed.return_value = FAKE_EMBEDDING

        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=mock_embed),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.search_tasks.return_value = []
            await search_tasks(
                "auth", status="todo,in-progress", include_someday=True, limit=5
            )

            MockStore.return_value.search_tasks.assert_called_once_with(
                query_embedding=FAKE_EMBEDDING,
                statuses=["todo", "in-progress"],
                include_someday=True,
                limit=5,
            )

    @pytest.mark.asyncio
    async def test_embedding_failure_returns_error(self):
        mock_embed = AsyncMock()
        mock_embed.embed.side_effect = RuntimeError("boom")

        with (
            patch("knowledge.mcp.Session"),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=mock_embed),
        ):
            result = await search_tasks("hello world")

        assert "error" in result


class TestUpdateTask:
    """Tests for the update_task MCP tool."""

    @pytest.mark.asyncio
    async def test_successful_update(self):
        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            result = await update_task("t1", {"status": "done"})

        assert result == {"updated": True, "note_id": "t1"}
        MockStore.return_value.patch_task.assert_called_once_with(
            "t1", {"status": "done"}
        )

    @pytest.mark.asyncio
    async def test_not_found_returns_error(self):
        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.patch_task.side_effect = ValueError(
                "Task not found: nope"
            )
            result = await update_task("nope", {"status": "done"})

        assert result == {"error": "Task not found: nope"}


class TestGetDailyTasks:
    """Tests for the get_daily_tasks MCP tool."""

    @pytest.mark.asyncio
    async def test_returns_daily_tasks(self):
        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.list_tasks_daily.return_value = CANNED_TASKS
            result = await get_daily_tasks()

        assert len(result["tasks"]) == 1
        MockStore.return_value.list_tasks_daily.assert_called_once()


class TestGetWeeklyTasks:
    """Tests for the get_weekly_tasks MCP tool."""

    @pytest.mark.asyncio
    async def test_returns_weekly_tasks(self):
        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.list_tasks_weekly.return_value = CANNED_TASKS
            result = await get_weekly_tasks()

        assert len(result["tasks"]) == 1
        MockStore.return_value.list_tasks_weekly.assert_called_once()


# ---------------------------------------------------------------------------
# Gardener decomposition tool tests (ADR 006 Phase 4c)
# ---------------------------------------------------------------------------


def _result(value):
    """Wrap a value as a SQLModel exec(...) result whose .first() returns it."""
    r = MagicMock()
    r.first.return_value = value
    return r


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["inspect_kg_review_pilot", "submit_kg_review_pilot"])
@pytest.mark.parametrize(
    "kind,authority,groups",
    [
        (PrincipalKind.HUMAN, Authority.ANONYMOUS, ("operators",)),
        (PrincipalKind.WORKLOAD, Authority.STANDING, ("operators",)),
        (PrincipalKind.HUMAN, Authority.DELEGATED, ("operators",)),
        (PrincipalKind.HUMAN, Authority.STANDING, ()),
    ],
)
async def test_review_pilot_denies_nonstanding_human_operator(
    tool, kind, authority, groups
):
    from dataclasses import replace
    from knowledge import mcp, review_pilot

    principal = replace(_principal(), kind=kind, authority=authority, groups=groups)
    with (
        patch.object(mcp, "current_principal", return_value=principal),
        patch.object(review_pilot, "inspect", new_callable=AsyncMock) as inspect,
        patch.object(review_pilot, "submit", new_callable=AsyncMock) as submit,
    ):
        args = (
            {}
            if tool == "inspect_kg_review_pilot"
            else {"job": "knowledge-review-admission-dry-run", "request_id": "bad"}
        )
        result = await getattr(mcp, tool)(**args)
    assert result == {"error": "standing human operator authority is required"}
    inspect.assert_not_called()
    submit.assert_not_called()


@pytest.mark.asyncio
async def test_review_pilot_carries_verified_operator_identity():
    from knowledge import mcp, review_pilot

    with (
        patch.object(mcp, "current_principal", return_value=_principal()),
        patch.object(
            review_pilot,
            "submit",
            new_callable=AsyncMock,
            return_value={"active": True},
        ) as submit,
    ):
        result = await mcp.submit_kg_review_pilot(
            "knowledge-review-admission-dry-run", "request"
        )
    assert result == {"active": True}
    submit.assert_awaited_once_with(
        "knowledge-review-admission-dry-run", "request", ":agent@example.com", None
    )
