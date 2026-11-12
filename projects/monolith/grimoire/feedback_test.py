"""In-app feedback: private intake, operator triage, and what reaches GitHub."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.exc import OperationalError
from auth.api import Authority, Principal, PrincipalKind
from core.db import get_session
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from grimoire import feedback, register
from grimoire.access import get_authenticated_identity
from grimoire.models import Feedback

ISSUER = "https://auth.example/application/o/grimoire/"
API = "/api/grimoire/feedback"
SECRET_TEXT = "The dice roller eats my modifier. Signed, a recognisable friend."


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_AUTH_ISSUER", ISSUER)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'feedback.db'}",
        connect_args={"check_same_thread": False},
    )
    schemas = {table: table.schema for table in SQLModel.metadata.tables.values()}
    for table in schemas:
        table.schema = None
    SQLModel.metadata.create_all(engine)
    app = FastAPI()
    register(app)

    def session():
        with Session(engine) as value:
            yield value

    app.dependency_overrides[get_session] = session

    def identity(request: Request):
        subject = request.headers.get("test-subject")
        if not subject:
            raise HTTPException(403, "missing identity")
        return Principal(
            subject=subject,
            issuer=ISSUER,
            display_name=subject,
            email=f"{subject}@example.test",
            email_verified=False,
            actor=(),
            scope=(),
            groups=("operators",) if subject == "admin" else (),
            kind=PrincipalKind.HUMAN,
            authority=Authority.STANDING,
        )

    app.dependency_overrides[get_authenticated_identity] = identity

    # No test reaches the network: an unexpected call fails loudly.
    def unexpected(*_args, **_kwargs):
        raise AssertionError("create_issue called unexpectedly")

    monkeypatch.setattr(feedback, "create_issue", unexpected)
    try:
        with TestClient(app) as client:
            for user in ("owner", "player", "admin"):
                assert (
                    client.get("/api/grimoire/lobby", headers=h(user)).status_code
                    == 200
                )
            created = client.post(
                "/api/grimoire/campaigns", headers=h("owner"), json={"name": "A table"}
            )
            assert created.status_code == 200
            yield client, engine, created.json()["id"]
    finally:
        for table, schema in schemas.items():
            table.schema = schema
        engine.dispose()


def h(user):
    return {"test-subject": user}


def send(client, user="player", **body):
    payload = {"text": SECRET_TEXT, "page": "/sheets", **body}
    return client.post(API, headers=h(user), json=payload)


def rows(engine):
    with Session(engine) as session:
        return [row.model_dump() for row in session.exec(select(Feedback)).all()]


@pytest.fixture
def github(monkeypatch):
    """Stand in for GitHub and record exactly what would be published."""
    calls = []

    def create_issue(repo, title, body, labels, *, user_agent):
        calls.append({"repo": repo, "title": title, "body": body, "labels": labels})
        return 4321

    monkeypatch.setattr(feedback, "create_issue", create_issue)
    return calls


def test_submit_stores_trimmed_text_and_answers_with_the_id_only(setup):
    client, engine, cid = setup
    response = send(
        client, "owner", text="  Rolls feel slow.  ", page=" /x ", campaign_id=cid
    )
    assert response.status_code == 201, response.text
    assert set(response.json()) == {"id"}
    (row,) = rows(engine)
    assert row["id"] == response.json()["id"]
    assert row["text"] == "Rolls feel slow."
    assert row["page"] == "/x"
    assert row["author_email"] == "owner@example.test"
    assert row["campaign_id"] == cid
    assert row["status"] == "new"
    assert row["issue_number"] is None and row["resolved_at"] is None
    assert isinstance(row["created_at"], datetime)


@pytest.mark.parametrize(
    "body",
    [
        {"text": ""},
        {"text": "   \n "},
        {"text": "x" * 2001},
        {"page": ""},
        {"page": "/" + "p" * 200},
        {"page": "https://elsewhere.example/"},
        {"page": "//elsewhere.example/"},
        {"page": "/\\elsewhere.example/"},
        {"page": "/sheets?private=value"},
        {"page": "/sheets#private"},
        {"page": "/a\nb"},
        {"campaign_id": "not-a-uuid"},
        {"author_email": "someone@example.test"},
    ],
)
def test_submit_rejects_invalid_bodies(setup, body):
    client, engine, _cid = setup
    assert send(client, **body).status_code == 422
    assert rows(engine) == []


def test_submit_accepts_the_longest_text_and_page(setup):
    client, _engine, _cid = setup
    response = send(client, text="x" * 2000, page="/" + "p" * 199)
    assert response.status_code == 201


def test_submit_requires_an_identity(setup):
    client, engine, _cid = setup
    response = client.post(API, json={"text": "hello", "page": "/"})
    assert response.status_code == 403
    assert rows(engine) == []


def test_a_campaign_the_sender_is_not_in_is_dropped_not_refused(setup):
    client, engine, cid = setup
    for campaign_id in (cid, str(uuid4())):
        response = send(client, "player", campaign_id=campaign_id)
        assert response.status_code == 201
    assert [row["campaign_id"] for row in rows(engine)] == [None, None]


def test_nul_characters_are_refused(setup):
    client, engine, _cid = setup
    assert send(client, text="a\x00b").status_code == 422
    assert send(client, page="/a\x00b").status_code == 422
    assert rows(engine) == []


def test_rate_limit_counts_one_author_over_a_rolling_day(setup):
    client, engine, _cid = setup
    for _ in range(feedback.DAILY_LIMIT):
        assert send(client).status_code == 201
    refused = send(client)
    assert refused.status_code == 429
    assert "Try again tomorrow" in refused.json()["detail"]
    # Another author is unaffected.
    assert send(client, "owner").status_code == 201
    # Rows older than a day no longer count.
    with Session(engine) as session:
        oldest = session.exec(
            select(Feedback).where(Feedback.author_email == "player@example.test")
        ).first()
        oldest.created_at = datetime.now(UTC) - timedelta(hours=25)
        session.commit()
    assert send(client).status_code == 201
    assert send(client).status_code == 429


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "", None),
        ("post", "/{id}/file", {"title": "T", "body": "B"}),
        ("post", "/{id}/dismiss", None),
    ],
)
def test_triage_is_for_operators_only(setup, method, path, body):
    client, engine, _cid = setup
    feedback_id = send(client).json()["id"]
    url = API + path.format(id=feedback_id)
    for user in ("player", "owner"):
        response = client.request(method, url, headers=h(user), json=body)
        assert response.status_code == 403
        assert response.json() == {"detail": "operator access required"}
    assert client.request(method, url, json=body).status_code == 403
    assert [row["status"] for row in rows(engine)] == ["new"]


def test_list_shows_new_items_newest_first_with_campaign_names(setup):
    client, engine, cid = setup
    first = send(client, "owner", text="first", campaign_id=cid).json()["id"]
    second = send(client, "player", text="second", page="/platform").json()["id"]
    with Session(engine) as session:
        session.get(Feedback, first).created_at = datetime.now(UTC) - timedelta(
            minutes=5
        )
        session.commit()
    response = client.get(API, headers=h("admin"))
    assert response.status_code == 200
    items = response.json()
    assert [item["id"] for item in items] == [second, first]
    assert items[1] == {
        "id": first,
        "created_at": items[1]["created_at"],
        "author": "owner@example.test",
        "campaign_id": cid,
        "campaign_name": "A table",
        "page": "/sheets",
        "text": "first",
        "status": "new",
        "issue_number": None,
    }
    assert datetime.fromisoformat(items[1]["created_at"]).tzinfo is not None
    assert items[0]["campaign_id"] is None and items[0]["campaign_name"] is None
    assert client.get(API + "?status=filed", headers=h("admin")).json() == []
    assert client.get(API + "?status=other", headers=h("admin")).status_code == 422


def test_list_is_bounded(setup, monkeypatch):
    client, _engine, _cid = setup
    monkeypatch.setattr(feedback, "LIST_LIMIT", 2)
    for _ in range(3):
        send(client)
    assert len(client.get(API, headers=h("admin")).json()) == 2


def test_lobby_counts_new_feedback_for_operators_only(setup):
    client, _engine, _cid = setup
    kept = send(client).json()["id"]
    dismissed = send(client).json()["id"]
    client.post(f"{API}/{dismissed}/dismiss", headers=h("admin"))

    def lobby(user):
        return client.get("/api/grimoire/lobby", headers=h(user)).json()

    assert lobby("admin")["new_feedback_count"] == 1
    assert lobby("admin")["can_administer_accounts"] is True
    assert lobby("player")["new_feedback_count"] == 0
    assert kept


def test_file_publishes_only_what_the_operator_wrote(setup, github):
    client, engine, cid = setup
    feedback_id = send(
        client, "owner", page="/campaigns/secret-page", campaign_id=cid
    ).json()["id"]
    response = client.post(
        f"{API}/{feedback_id}/file",
        headers=h("admin"),
        json={"title": "  Dice modifier is dropped  ", "body": "  Steps to see it.  "},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "issue_number": 4321,
        "issue_url": f"https://github.com/{feedback.GITHUB_REPO}/issues/4321",
    }
    assert github == [
        {
            "repo": feedback.GITHUB_REPO,
            "title": "Dice modifier is dropped",
            "body": "Steps to see it.\n\nFiled from Grimoire feedback.",
            "labels": ["grimoire", "feedback"],
        }
    ]
    published = repr(github)
    for private in (SECRET_TEXT, "owner@example.test", "owner", cid, "A table"):
        assert private not in published
    assert "secret-page" not in published
    (row,) = rows(engine)
    assert row["status"] == "filed"
    assert row["issue_number"] == 4321
    assert isinstance(row["resolved_at"], datetime)
    assert row["text"] == SECRET_TEXT
    (filed,) = client.get(API + "?status=filed", headers=h("admin")).json()
    assert filed["issue_number"] == 4321


@pytest.mark.parametrize(
    "body",
    [
        {"title": "", "body": "B"},
        {"title": "T" * 121, "body": "B"},
        {"title": "T", "body": " "},
        {"title": "T", "body": "B" * 4001},
        {"title": "T", "body": "B", "labels": ["agent-ready"]},
    ],
)
def test_file_rejects_invalid_bodies_without_calling_github(setup, github, body):
    client, engine, _cid = setup
    feedback_id = send(client).json()["id"]
    response = client.post(f"{API}/{feedback_id}/file", headers=h("admin"), json=body)
    assert response.status_code == 422
    assert github == []
    assert [row["status"] for row in rows(engine)] == ["new"]


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectTimeout("timed out"),
        httpx.HTTPStatusError(
            "refused",
            request=httpx.Request("POST", "https://api.github.com/x"),
            response=httpx.Response(403),
        ),
        ValueError("GitHub issue response had an unexpected shape"),
    ],
)
def test_github_failure_leaves_the_feedback_new(setup, monkeypatch, error):
    client, engine, _cid = setup
    feedback_id = send(client).json()["id"]

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(feedback, "create_issue", fail)
    response = client.post(
        f"{API}/{feedback_id}/file",
        headers=h("admin"),
        json={"title": "T", "body": "B"},
    )
    assert response.status_code == 502
    assert response.json() == {
        "detail": "GitHub did not confirm the issue. "
        "Check the repository before filing again."
    }
    (row,) = rows(engine)
    assert row["status"] == "new"
    assert row["issue_number"] is None and row["resolved_at"] is None
    assert len(client.get(API, headers=h("admin")).json()) == 1


def test_post_publication_commit_failure_warns_before_any_retry(
    setup, github, monkeypatch
):
    client, engine, _cid = setup
    feedback_id = send(client).json()["id"]
    commit = Session.commit

    def fail_commit(session):
        if github:
            raise OperationalError("commit", {}, RuntimeError("database unavailable"))
        return commit(session)

    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", fail_commit)
        response = client.post(
            f"{API}/{feedback_id}/file",
            headers=h("admin"),
            json={"title": "T", "body": "B"},
        )
    assert response.status_code == 502
    assert response.json() == {
        "detail": "GitHub created the issue but saving its resolution failed. "
        "Check the repository before filing again."
    }
    assert len(github) == 1
    (row,) = rows(engine)
    assert row["status"] == "new"
    assert row["issue_number"] is None and row["resolved_at"] is None


def test_a_resolved_item_cannot_be_filed_or_dismissed_again(setup, github):
    client, engine, _cid = setup
    filed = send(client).json()["id"]
    body = {"title": "T", "body": "B"}
    assert client.post(f"{API}/{filed}/file", headers=h("admin"), json=body).is_success
    again = client.post(f"{API}/{filed}/file", headers=h("admin"), json=body)
    assert again.status_code == 409
    assert again.json() == {"detail": "feedback is already filed"}
    assert client.post(f"{API}/{filed}/dismiss", headers=h("admin")).status_code == 409
    assert len(github) == 1
    (row,) = rows(engine)
    assert row["status"] == "filed" and row["issue_number"] == 4321


def test_dismiss_resolves_without_publishing(setup):
    client, engine, _cid = setup
    feedback_id = send(client).json()["id"]
    response = client.post(f"{API}/{feedback_id}/dismiss", headers=h("admin"))
    assert response.status_code == 204
    (row,) = rows(engine)
    assert row["status"] == "dismissed"
    assert row["issue_number"] is None
    assert isinstance(row["resolved_at"], datetime)
    assert client.get(API, headers=h("admin")).json() == []
    again = client.post(f"{API}/{feedback_id}/dismiss", headers=h("admin"))
    assert again.status_code == 409
    body = {"title": "T", "body": "B"}
    filed = client.post(f"{API}/{feedback_id}/file", headers=h("admin"), json=body)
    assert filed.status_code == 409


def test_unknown_or_malformed_ids(setup):
    client, _engine, _cid = setup
    missing = client.post(f"{API}/{uuid4()}/dismiss", headers=h("admin"))
    assert missing.status_code == 404
    assert client.post(f"{API}/nope/dismiss", headers=h("admin")).status_code == 422
