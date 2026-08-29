from __future__ import annotations

import asyncio
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlmodel import Session, create_engine

from auth.api import Authority, Principal, PrincipalKind
from auth.dependencies import reset_current_principal, set_current_principal
from core.db import get_session
from updates import mcp, router, store
from updates.models import ProductUpdate
from updates.schemas import ProductUpdateSubmission, Project, Technology


@pytest.fixture(name="engine")
def engine_fixture(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'updates.db'}",
        connect_args={"check_same_thread": False},
    )
    table = ProductUpdate.__table__
    schema = table.schema
    table.schema = None
    try:
        table.create(engine)
        yield engine
    finally:
        table.schema = schema


def principal(*scopes: str) -> Principal:
    return Principal(
        subject="workload:daily-updates",
        actor=(),
        scope=tuple(scopes),
        groups=(),
        email=None,
        kind=PrincipalKind.WORKLOAD,
        authority=Authority.STANDING,
    )


def submission(**overrides) -> ProductUpdateSubmission:
    values = {
        "published_on": date(2026, 8, 29),
        "category": "new-feature",
        "headline": "Private product updates",
        "summary": "A daily release journal is now available on the private site.",
        "highlights": [
            {
                "title": "Daily journal",
                "description": "Browse structured updates by month and day.",
            }
        ],
        "improvements": [],
        "projects": ["monolith"],
        "technologies": ["frontend", "agents"],
        "source_base_sha": "a" * 40,
        "source_head_sha": "b" * 40,
        "source_commit_count": 7,
    }
    values.update(overrides)
    return ProductUpdateSubmission(**values)


def run_as(identity: Principal, awaitable):
    token = set_current_principal(identity)
    try:
        return asyncio.run(awaitable)
    finally:
        reset_current_principal(token)


def test_submission_rejects_unknown_fields_duplicate_facets_and_short_shas():
    with pytest.raises(ValidationError):
        submission(unexpected="value")
    with pytest.raises(ValidationError):
        submission(projects=["monolith", "monolith"])
    with pytest.raises(ValidationError):
        submission(source_head_sha="abc")


def test_publish_is_immutable_and_exact_replays_are_idempotent(engine):
    update = submission()
    with Session(engine) as session:
        first, created = store.publish_update(update, principal(), session=session)
        replay, replay_created = store.publish_update(
            update, principal(), session=session
        )

        assert created is True
        assert replay_created is False
        assert replay.published_on == first.published_on
        with pytest.raises(store.UpdateAlreadyPublished, match="already has"):
            store.publish_update(
                submission(headline="A conflicting update"),
                principal(),
                session=session,
            )


def test_archive_filters_entries_and_keeps_unfiltered_facet_counts(engine):
    with Session(engine) as session:
        store.publish_update(submission(), principal(), session=session)
        store.publish_update(
            submission(
                published_on=date(2026, 8, 28),
                category="improvement",
                headline="Storage improvements",
                projects=["home-cluster"],
                technologies=["storage"],
                source_base_sha="c" * 40,
                source_head_sha="d" * 40,
                source_commit_count=2,
            ),
            principal(),
            session=session,
        )

        result = store.archive(
            project=Project.MONOLITH,
            technology=Technology.FRONTEND,
            session=session,
        )

    assert [item.headline for item in result.updates] == ["Private product updates"]
    assert result.updates[0].source_compare_url == (
        f"https://github.com/jomcgi-org/homelab/compare/{'a' * 40}...{'b' * 40}"
    )
    assert {facet.value: facet.count for facet in result.projects} == {
        "home-cluster": 1,
        "monolith": 1,
    }
    assert {facet.value: facet.count for facet in result.technologies} == {
        "agents": 1,
        "frontend": 1,
        "storage": 1,
    }


def test_private_archive_route_uses_validated_facets(engine):
    app = FastAPI()
    app.include_router(router.router)

    def session_override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    with Session(engine) as session:
        store.publish_update(submission(), principal(), session=session)

    with TestClient(app) as client:
        response = client.get("/api/updates?project=monolith")
        invalid = client.get("/api/updates?technology=made-up")

    assert response.status_code == 200
    assert response.json()["updates"][0]["headline"] == "Private product updates"
    assert invalid.status_code == 422


def test_submission_tool_requires_the_workload_scope():
    result = run_as(principal(), mcp.submit_product_update(submission()))

    assert result == {
        "accepted": False,
        "error": "caller requires the 'updates:submit' scope",
    }


def test_submission_tool_returns_private_page_and_source_links(monkeypatch):
    row = ProductUpdate(
        **submission().model_dump(mode="python"),
        submitted_by="workload:daily-updates",
        submitted_authority="standing",
    )
    monkeypatch.setattr(mcp, "publish_update", lambda update, actor: (row, True))

    result = run_as(
        principal(mcp.SUBMIT_SCOPE), mcp.submit_product_update(submission())
    )

    assert result["accepted"] is True
    assert result["created"] is True
    assert result["url"] == ("https://private.jomcgi.dev/updates#update-2026-08-29")
    assert result["source_compare_url"].endswith(f"{'a' * 40}...{'b' * 40}")
