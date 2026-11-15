"""In-app feedback: intake, triage, the shared roadmap, and what reaches GitHub."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
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
from grimoire.models import Feedback, FeedbackVote

ISSUER = "https://auth.example/application/o/grimoire/"
API = "/api/grimoire/feedback"
ROADMAP = API + "/roadmap"
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

    def unexpected_read(*_args, **_kwargs):
        raise AssertionError("list_issue_states called unexpectedly")

    monkeypatch.setattr(feedback, "list_issue_states", unexpected_read)
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
    assert row["shared"] is True
    assert row["title"] is None and row["reason"] is None
    assert row["issue_state"] is None and row["issue_checked_at"] is None


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
        "votes": 0,
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
    assert row["title"] == "Dice modifier is dropped"
    assert row["issue_state"] == "open"
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
        httpx.ReadTimeout("GitHub accepted the issue but its response was lost"),
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


# --- The roadmap ---------------------------------------------------------

ROADMAP_KEYS = {
    "id",
    "state",
    "text",
    "votes",
    "voted",
    "mine",
    "created_at",
    "issue_url",
    "reason",
}
ISSUE_URL = f"https://github.com/{feedback.GITHUB_REPO}/issues/4321"


def roadmap(client, user):
    response = client.get(ROADMAP, headers=h(user))
    assert response.status_code == 200, response.text
    return {item["id"]: item for item in response.json()}


def edit(engine, feedback_id, **values):
    with Session(engine) as session:
        row = session.get(Feedback, feedback_id)
        for name, value in values.items():
            setattr(row, name, value)
        session.commit()


def file(client, feedback_id, title="Dice modifier is dropped"):
    response = client.post(
        f"{API}/{feedback_id}/file",
        headers=h("admin"),
        json={"title": title, "body": "B"},
    )
    assert response.status_code == 200, response.text


@pytest.fixture
def issues(monkeypatch):
    """Stand in for the GitHub issue list: set `states`, read `calls`."""

    fake = SimpleNamespace(states={}, error=None, calls=[])

    def list_issue_states(repo, labels, *, user_agent):
        fake.calls.append({"repo": repo, "labels": labels})
        if fake.error:
            raise fake.error
        return {**fake.states}

    monkeypatch.setattr(feedback, "list_issue_states", list_issue_states)
    return fake


def test_roadmap_requires_an_identity(setup):
    client, _engine, _cid = setup
    send(client)
    assert client.get(ROADMAP).status_code == 403


def test_roadmap_shows_a_shared_note_to_everyone_without_its_sender(setup):
    client, _engine, cid = setup
    feedback_id = send(
        client, "owner", page="/campaigns/secret-page", campaign_id=cid
    ).json()["id"]
    response = client.get(ROADMAP, headers=h("player"))
    (item,) = response.json()
    assert set(item) == ROADMAP_KEYS
    assert item == {
        "id": feedback_id,
        "state": "new",
        "text": SECRET_TEXT,
        "votes": 0,
        "voted": False,
        "mine": False,
        "created_at": item["created_at"],
        "issue_url": None,
        "reason": None,
    }
    assert datetime.fromisoformat(item["created_at"]).tzinfo is not None
    for private in ("owner@example.test", "secret-page", cid, "A table"):
        assert private not in response.text
    assert roadmap(client, "owner")[feedback_id]["mine"] is True


def test_roadmap_is_newest_first_and_bounded(setup, monkeypatch):
    client, engine, _cid = setup
    first = send(client, text="first").json()["id"]
    second = send(client, text="second").json()["id"]
    edit(engine, first, created_at=datetime.now(UTC) - timedelta(minutes=5))
    assert list(roadmap(client, "owner")) == [second, first]
    monkeypatch.setattr(feedback, "ROADMAP_LIMIT", 1)
    assert list(roadmap(client, "owner")) == [second]


def test_an_unshared_note_is_visible_only_to_its_author(setup):
    client, engine, _cid = setup
    feedback_id = send(client, "player").json()["id"]
    edit(engine, feedback_id, shared=False)
    assert roadmap(client, "owner") == {}
    assert roadmap(client, "admin") == {}
    mine = roadmap(client, "player")[feedback_id]
    assert mine["text"] == SECRET_TEXT and mine["mine"] is True
    # Dismissed, it still shows only to the person who wrote it.
    client.post(
        f"{API}/{feedback_id}/dismiss", headers=h("admin"), json={"reason": "No."}
    )
    assert roadmap(client, "owner") == {}
    mine = roadmap(client, "player")[feedback_id]
    assert mine["state"] == "rejected" and mine["reason"] == "No."


def test_an_unshared_filed_note_shows_others_only_the_operator_title(
    setup, github, issues
):
    client, engine, _cid = setup
    feedback_id = send(client, "player").json()["id"]
    edit(engine, feedback_id, shared=False)
    file(client, feedback_id)
    response = client.get(ROADMAP, headers=h("owner"))
    assert SECRET_TEXT not in response.text
    (item,) = response.json()
    assert item["text"] == "Dice modifier is dropped"
    assert item["state"] == "accepted"
    assert item["issue_url"] == ISSUE_URL
    assert item["mine"] is False


def test_a_filed_note_without_a_title_is_a_plain_suggestion_to_others(
    setup, github, issues
):
    client, engine, _cid = setup
    feedback_id = send(client, "player").json()["id"]
    file(client, feedback_id)
    # A row filed before titles were stored, sent before notes were shared.
    edit(engine, feedback_id, shared=False, title=None, issue_state=None)
    response = client.get(ROADMAP, headers=h("owner"))
    assert SECRET_TEXT not in response.text
    (item,) = response.json()
    assert item["text"] == "Suggestion"
    assert item["state"] == "accepted"
    assert item["issue_url"] == ISSUE_URL
    assert roadmap(client, "player")[feedback_id]["text"] == SECRET_TEXT


def test_a_shared_filed_note_shows_the_operator_title_to_everyone(
    setup, github, issues
):
    client, _engine, _cid = setup
    feedback_id = send(client, "player").json()["id"]
    file(client, feedback_id, title="Keep the modifier")
    for user in ("owner", "player"):
        assert roadmap(client, user)[feedback_id]["text"] == "Keep the modifier"


def test_a_dismissed_shared_note_is_rejected_with_its_reason(setup):
    client, engine, _cid = setup
    feedback_id = send(client, "player").json()["id"]
    response = client.post(
        f"{API}/{feedback_id}/dismiss",
        headers=h("admin"),
        json={"reason": "  Covered by the sheet rework.  "},
    )
    assert response.status_code == 204
    (row,) = rows(engine)
    assert row["reason"] == "Covered by the sheet rework."
    item = roadmap(client, "owner")[feedback_id]
    assert item["state"] == "rejected"
    assert item["reason"] == "Covered by the sheet rework."
    assert item["issue_url"] is None
    assert item["text"] == SECRET_TEXT


@pytest.mark.parametrize("body", [None, {}, {"reason": None}, {"reason": "   "}])
def test_dismiss_reason_is_optional(setup, body):
    client, engine, _cid = setup
    feedback_id = send(client).json()["id"]
    response = client.post(
        f"{API}/{feedback_id}/dismiss", headers=h("admin"), json=body
    )
    assert response.status_code == 204
    (row,) = rows(engine)
    assert row["status"] == "dismissed" and row["reason"] is None
    assert roadmap(client, "owner")[feedback_id]["reason"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"reason": "x" * 201},
        {"reason": "a\x00b"},
        {"reason": "two\nlines"},
        {"reason": 7},
        {"reason": "fine", "status": "filed"},
        ["fine"],
    ],
)
def test_dismiss_rejects_invalid_reasons(setup, body):
    client, engine, _cid = setup
    feedback_id = send(client).json()["id"]
    response = client.post(
        f"{API}/{feedback_id}/dismiss", headers=h("admin"), json=body
    )
    assert response.status_code == 422
    assert [row["status"] for row in rows(engine)] == ["new"]


def test_dismiss_accepts_the_longest_reason(setup):
    client, engine, _cid = setup
    feedback_id = send(client).json()["id"]
    response = client.post(
        f"{API}/{feedback_id}/dismiss", headers=h("admin"), json={"reason": "x" * 200}
    )
    assert response.status_code == 204
    assert rows(engine)[0]["reason"] == "x" * 200


# --- Votes ---------------------------------------------------------------


def vote(client, user, feedback_id, method="put"):
    return client.request(method, f"{API}/{feedback_id}/vote", headers=h(user))


def test_votes_are_idempotent_and_counted_per_voter(setup):
    client, engine, _cid = setup
    feedback_id = send(client, "player").json()["id"]
    for _ in range(2):
        response = vote(client, "owner", feedback_id)
        assert response.status_code == 200, response.text
        assert response.json() == {"votes": 1, "voted": True}
    assert vote(client, "admin", feedback_id).json() == {"votes": 2, "voted": True}
    with Session(engine) as session:
        assert sorted(v.voter_email for v in session.exec(select(FeedbackVote))) == [
            "admin@example.test",
            "owner@example.test",
        ]
    assert roadmap(client, "owner")[feedback_id]["votes"] == 2
    assert roadmap(client, "owner")[feedback_id]["voted"] is True
    assert roadmap(client, "player")[feedback_id]["voted"] is False
    # The operator inbox carries the count for triage.
    (item,) = client.get(API, headers=h("admin")).json()
    assert item["votes"] == 2
    for _ in range(2):
        response = vote(client, "owner", feedback_id, "delete")
        assert response.status_code == 200
        assert response.json() == {"votes": 1, "voted": False}
    assert roadmap(client, "owner")[feedback_id]["votes"] == 1
    assert roadmap(client, "owner")[feedback_id]["voted"] is False


@pytest.mark.parametrize("method", ["put", "delete"])
def test_an_author_cannot_vote_for_their_own_note(setup, method):
    client, engine, _cid = setup
    feedback_id = send(client, "player").json()["id"]
    response = vote(client, "player", feedback_id, method)
    assert response.status_code == 409
    assert response.json() == {"detail": "This one is yours."}
    with Session(engine) as session:
        assert session.exec(select(FeedbackVote)).all() == []


@pytest.mark.parametrize("method", ["put", "delete"])
def test_votes_need_an_identity_and_a_visible_note(setup, method):
    client, engine, _cid = setup
    feedback_id = send(client, "player").json()["id"]
    assert client.request(method, f"{API}/{feedback_id}/vote").status_code == 403
    assert vote(client, "owner", uuid4(), method).status_code == 404
    assert vote(client, "owner", "nope", method).status_code == 422
    edit(engine, feedback_id, shared=False)
    hidden = vote(client, "owner", feedback_id, method)
    assert hidden.status_code == 404
    assert hidden.json() == {"detail": "feedback not found"}
    with Session(engine) as session:
        assert session.exec(select(FeedbackVote)).all() == []


def test_votes_stay_open_on_accepted_and_close_on_resolved(setup, github, issues):
    client, engine, _cid = setup
    accepted = send(client, "player").json()["id"]
    file(client, accepted)
    assert vote(client, "owner", accepted).json() == {"votes": 1, "voted": True}
    dismissed = send(client, "player").json()["id"]
    client.post(f"{API}/{dismissed}/dismiss", headers=h("admin"))
    for state in ("completed", "not_planned"):
        edit(engine, accepted, issue_state=state)
        for feedback_id in (accepted, dismissed):
            for method in ("put", "delete"):
                response = vote(client, "owner", feedback_id, method)
                assert response.status_code == 409
                assert response.json() == {"detail": "Voting has closed on this one."}
    # The vote cast while it was open is kept.
    assert roadmap(client, "owner")[accepted]["votes"] == 1


# --- Issue states read back from GitHub ------------------------------------


@pytest.mark.parametrize(
    "issue_state,state",
    [("open", "accepted"), ("completed", "completed"), ("not_planned", "rejected")],
)
def test_roadmap_state_follows_the_github_issue(
    setup, github, issues, issue_state, state
):
    client, engine, _cid = setup
    feedback_id = send(client, "player").json()["id"]
    file(client, feedback_id)
    issues.states = {4321: issue_state, 99: "completed"}
    item = roadmap(client, "owner")[feedback_id]
    assert item["state"] == state
    assert item["issue_url"] == ISSUE_URL
    assert item["reason"] is None
    assert issues.calls == [
        {"repo": feedback.GITHUB_REPO, "labels": ["grimoire", "feedback"]}
    ]
    (row,) = rows(engine)
    assert row["issue_state"] == issue_state
    assert isinstance(row["issue_checked_at"], datetime)
    # The triage status is unchanged: GitHub owns only the issue state.
    assert row["status"] == "filed"


def test_roadmap_without_filed_notes_never_calls_github(setup):
    client, _engine, _cid = setup
    feedback_id = send(client).json()["id"]
    client.post(f"{API}/{feedback_id}/dismiss", headers=h("admin"))
    send(client)
    # The fixture's stand-in fails the test if GitHub is read.
    assert len(roadmap(client, "owner")) == 2


def test_issue_states_are_read_at_most_once_per_interval(
    setup, github, issues, monkeypatch
):
    client, engine, _cid = setup
    feedback_id = send(client, "player").json()["id"]
    file(client, feedback_id)
    now = datetime.now(UTC)
    monkeypatch.setattr(feedback, "utc_now", lambda: now)
    for user in ("owner", "player", "owner"):
        assert roadmap(client, user)[feedback_id]["state"] == "accepted"
    assert len(issues.calls) == 1
    issues.states = {4321: "completed"}
    now += timedelta(minutes=9, seconds=59)
    assert roadmap(client, "owner")[feedback_id]["state"] == "accepted"
    assert len(issues.calls) == 1
    now += timedelta(seconds=2)
    assert roadmap(client, "owner")[feedback_id]["state"] == "completed"
    assert len(issues.calls) == 2
    # An issue missing from the answer keeps its stored state.
    issues.states = {}
    now += timedelta(minutes=11)
    assert roadmap(client, "owner")[feedback_id]["state"] == "completed"
    assert len(issues.calls) == 3
    # A note filed since the last read triggers one straight away.
    second = send(client, "player").json()["id"]
    file(client, second)
    edit(engine, second, issue_number=4322)
    issues.states = {4322: "not_planned"}
    assert roadmap(client, "owner")[second]["state"] == "rejected"
    assert len(issues.calls) == 4


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectTimeout("timed out"),
        httpx.HTTPStatusError(
            "refused",
            request=httpx.Request("GET", "https://api.github.com/x"),
            response=httpx.Response(403),
        ),
        ValueError("GitHub issue list had an unexpected shape"),
    ],
)
def test_github_failure_serves_the_stored_states(
    setup, github, issues, monkeypatch, caplog, error
):
    client, engine, _cid = setup
    feedback_id = send(client, "player").json()["id"]
    file(client, feedback_id)
    edit(engine, feedback_id, issue_state="completed")
    now = datetime.now(UTC)
    monkeypatch.setattr(feedback, "utc_now", lambda: now)
    issues.error = error
    with caplog.at_level("WARNING", logger="monolith.grimoire.feedback"):
        assert roadmap(client, "owner")[feedback_id]["state"] == "completed"
        # The failed attempt is throttled like a successful one.
        assert roadmap(client, "owner")[feedback_id]["state"] == "completed"
    assert len(issues.calls) == 1
    assert [record.getMessage() for record in caplog.records] == [
        f"grimoire feedback: issue states not refreshed ({type(error).__name__})"
    ]
    issues.error = None
    issues.states = {4321: "open"}
    now += timedelta(minutes=11)
    assert roadmap(client, "owner")[feedback_id]["state"] == "accepted"


def test_a_database_failure_while_saving_states_still_serves_the_roadmap(
    setup, github, issues, monkeypatch
):
    client, _engine, _cid = setup
    feedback_id = send(client, "player").json()["id"]
    file(client, feedback_id)
    issues.states = {4321: "completed"}
    commit = Session.commit

    def fail_commit(session):
        if issues.calls:
            raise OperationalError("commit", {}, RuntimeError("database unavailable"))
        return commit(session)

    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", fail_commit)
        assert roadmap(client, "owner")[feedback_id]["state"] == "accepted"
    assert len(issues.calls) == 1
