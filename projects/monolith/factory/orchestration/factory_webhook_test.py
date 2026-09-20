"""Hermetic authentication, trust, replay and retry tests for factory intake."""

from __future__ import annotations

import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
from threading import Event as ThreadEvent

import pytest
from core.db import get_session
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from factory.orchestration import factory_webhook as webhook
from factory.orchestration.factory_models import (
    FactoryGithubIssueState,
    FactoryReceipt,
    FactoryWebhookDelivery,
    WorkItem,
    WorkItemEdge,
    WorkItemEvent,
)
from factory.orchestration.models import SwarmTask
from factory.orchestration.work_items import (
    mint_or_sync_from_github,
    set_authority_local,
)

SECRET = "factory-test-secret"


def _issue(
    *,
    number=6257,
    login="jomcgi",
    user_type="User",
    title="Webhook item",
    body="public issue body",
    labels=("agent-ready",),
    updated_at="2026-09-20T10:00:00Z",
):
    return {
        "number": number,
        "title": title,
        "body": body,
        "html_url": f"https://github.com/owner/repo/issues/{number}",
        "state": "open",
        "labels": [{"name": label} for label in labels],
        "user": {"login": login, "type": user_type},
        "created_at": "2026-09-20T10:00:00Z",
        **({"updated_at": updated_at} if updated_at is not None else {}),
    }


def _payload(*, action="opened", issue=None, repo="owner/repo"):
    issue = issue or _issue()
    return {
        "action": action,
        "issue": issue,
        "repository": {"full_name": repo},
        "sender": {"login": "delivery-sender"},
    }


def _headers(body: bytes, *, event_name="issues", delivery="delivery-1"):
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-GitHub-Event": event_name,
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": f"sha256={digest}",
    }


def _post(client, payload, *, event_name="issues", delivery="delivery-1"):
    body = json.dumps(payload, separators=(",", ":")).encode()
    return client.post(
        "/webhooks/github/factory",
        content=body,
        headers=_headers(body, event_name=event_name, delivery=delivery),
    )


def _setup(tmp_path, monkeypatch):
    database = tmp_path / "factory-webhook.db"
    engine = create_engine(
        f"sqlite:///{database}",
        connect_args={"check_same_thread": False, "timeout": 10},
        execution_options={"schema_translate_map": {"swarm": None}},
    )

    @event.listens_for(engine, "connect")
    def configure(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")

    SQLModel.metadata.create_all(
        engine,
        tables=[
            model.__table__
            for model in (
                SwarmTask,
                WorkItem,
                WorkItemEdge,
                WorkItemEvent,
                FactoryReceipt,
                FactoryWebhookDelivery,
                FactoryGithubIssueState,
            )
        ],
    )
    monkeypatch.setenv("FACTORY_GITHUB_WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("FACTORY_GITHUB_WEBHOOK_REPOSITORY", "owner/repo")
    monkeypatch.setenv("FACTORY_GITHUB_WEBHOOK_TRUSTED_AUTHORS", "jomcgi")
    monkeypatch.setenv("FACTORY_GITHUB_WEBHOOK_SECRET", SECRET)

    app = FastAPI()
    app.include_router(webhook.router)

    def session_override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    return engine, TestClient(app, raise_server_exceptions=False)


def test_disabled_missing_secret_and_invalid_signature_fail_closed(
    tmp_path, monkeypatch
):
    _engine, client = _setup(tmp_path, monkeypatch)
    payload = _payload()
    body = json.dumps(payload).encode()

    monkeypatch.setenv("FACTORY_GITHUB_WEBHOOK_ENABLED", "false")
    assert client.post("/webhooks/github/factory", content=body).status_code == 404

    monkeypatch.setenv("FACTORY_GITHUB_WEBHOOK_ENABLED", "true")
    monkeypatch.delenv("FACTORY_GITHUB_WEBHOOK_SECRET")
    assert (
        client.post(
            "/webhooks/github/factory", content=body, headers=_headers(body)
        ).status_code
        == 401
    )

    monkeypatch.setenv("FACTORY_GITHUB_WEBHOOK_SECRET", SECRET)
    headers = _headers(body)
    headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64
    assert (
        client.post(
            "/webhooks/github/factory", content=body, headers=headers
        ).status_code
        == 401
    )

    headers = _headers(body, delivery="wrong-content-type")
    headers["Content-Type"] = "text/plain"
    assert (
        client.post(
            "/webhooks/github/factory", content=body, headers=headers
        ).status_code
        == 415
    )


def test_event_repository_and_issue_identity_are_validated(tmp_path, monkeypatch):
    engine, client = _setup(tmp_path, monkeypatch)
    assert _post(client, _payload(repo="elsewhere/repo")).status_code == 403

    mismatch = _payload()
    mismatch["issue"]["number"] = "6257"
    assert _post(client, mismatch, delivery="bad-identity").status_code == 422

    comment = _post(
        client,
        _payload(action="created"),
        event_name="issue_comment",
        delivery="comment-1",
    )
    assert comment.status_code == 200
    assert comment.json() == {"status": "ignored", "reason": "unsupported_event"}
    with Session(engine) as session:
        assert session.exec(select(WorkItem)).all() == []

    genuine_issues_shape = _payload()
    assert "number" not in genuine_issues_shape
    accepted = _post(client, genuine_issues_shape, delivery="real-issues-shape")
    assert accepted.status_code == 200
    assert accepted.json()["outcome"] == "trusted_minted"


def test_payload_size_is_bounded_before_processing(tmp_path, monkeypatch):
    _engine, client = _setup(tmp_path, monkeypatch)
    body = b"{" + b"x" * webhook.MAX_PAYLOAD_BYTES + b"}"
    response = client.post(
        "/webhooks/github/factory",
        content=body,
        headers=_headers(body, delivery="oversized-delivery"),
    )
    assert response.status_code == 413


def test_trust_boundaries_and_stable_work_item_identity(tmp_path, monkeypatch):
    engine, client = _setup(tmp_path, monkeypatch)

    trusted = _post(client, _payload(), delivery="trusted-open")
    assert trusted.status_code == 200
    item_id = trusted.json()["work_item_id"]

    edited = _post(
        client,
        _payload(
            action="edited",
            issue=_issue(title="Revised title", updated_at="2026-09-20T10:01:00Z"),
        ),
        delivery="trusted-edit",
    )
    assert edited.status_code == 200
    assert edited.json()["work_item_id"] == item_id

    bot = _post(
        client,
        _payload(issue=_issue(login="dependabot[bot]", user_type="Bot")),
        delivery="bot-open",
    )
    outsider = _post(
        client,
        _payload(issue=_issue(login="outsider")),
        delivery="outsider-open",
    )
    assert bot.json()["outcome"] == "semi_trusted_held"
    assert outsider.json()["outcome"] == "untrusted_ignored"

    with Session(engine) as session:
        items = session.exec(select(WorkItem)).all()
        assert len(items) == 1
        assert items[0].id == item_id
        assert items[0].title == "Revised title"
        assert items[0].trust == "trusted"


def test_issue_close_is_a_lifecycle_transition_not_a_comment(tmp_path, monkeypatch):
    engine, client = _setup(tmp_path, monkeypatch)
    opened = _post(client, _payload(), delivery="lifecycle-open")
    item_id = opened.json()["work_item_id"]
    closed_issue = _issue()
    closed_issue["state"] = "closed"
    closed_issue["updated_at"] = "2026-09-20T10:01:00Z"

    closed = _post(
        client,
        _payload(action="closed", issue=closed_issue),
        delivery="lifecycle-close",
    )
    assert closed.status_code == 200
    assert closed.json()["outcome"] == "trusted_closed"
    with Session(engine) as session:
        item = session.get(WorkItem, item_id)
        assert item.state == "closed"
        assert item.close_reason == "github_closed"


def test_close_before_import_persists_watermark_and_rejects_stale_open(
    tmp_path, monkeypatch
):
    engine, client = _setup(tmp_path, monkeypatch)
    closed_issue = _issue(updated_at="2026-09-20T10:02:00Z")
    closed_issue["state"] = "closed"

    closed = _post(
        client,
        _payload(action="closed", issue=closed_issue),
        delivery="close-before-import",
    )
    stale = _post(
        client,
        _payload(
            action="edited",
            issue=_issue(title="Stale open", updated_at="2026-09-20T10:01:00Z"),
        ),
        delivery="distinct-stale-open",
    )

    assert closed.json()["outcome"] == "trusted_closed"
    assert stale.json()["outcome"] == "trusted_stale_ignored"
    with Session(engine) as session:
        assert session.exec(select(WorkItem)).all() == []
        source = session.get(FactoryGithubIssueState, ("owner/repo", 6257))
        assert source.source_state == "closed"
        assert (
            source.source_updated_at.replace(tzinfo=timezone.utc).isoformat()
            == "2026-09-20T10:02:00+00:00"
        )
        delivery = session.get(FactoryWebhookDelivery, "distinct-stale-open")
        assert (
            delivery.source_updated_at.replace(tzinfo=timezone.utc).isoformat()
            == "2026-09-20T10:01:00+00:00"
        )


def test_closed_item_rejects_stale_equal_and_missing_but_later_reopens(
    tmp_path, monkeypatch
):
    engine, client = _setup(tmp_path, monkeypatch)
    opened = _post(client, _payload(), delivery="ordered-open")
    item_id = opened.json()["work_item_id"]
    closed_issue = _issue(updated_at="2026-09-20T10:02:00Z")
    closed_issue["state"] = "closed"
    assert (
        _post(
            client,
            _payload(action="closed", issue=closed_issue),
            delivery="ordered-close",
        ).json()["outcome"]
        == "trusted_closed"
    )

    for delivery, updated_at in (
        ("stale-distinct-edit", "2026-09-20T10:01:00Z"),
        ("equal-distinct-reopen", "2026-09-20T10:02:00Z"),
    ):
        response = _post(
            client,
            _payload(
                action="reopened",
                issue=_issue(title="Must not win", updated_at=updated_at),
            ),
            delivery=delivery,
        )
        assert response.json()["outcome"] == "trusted_stale_ignored"

    missing = _post(
        client,
        _payload(
            action="edited",
            issue=_issue(title="No source order", updated_at=None),
        ),
        delivery="missing-source-time",
    )
    assert missing.json()["outcome"] == "trusted_missing_timestamp_ignored"

    reopened = _post(
        client,
        _payload(
            action="reopened",
            issue=_issue(title="Legitimate reopen", updated_at="2026-09-20T10:03:00Z"),
        ),
        delivery="later-reopen",
    )
    assert reopened.json()["outcome"] == "trusted_synced"

    with Session(engine) as session:
        item = session.get(WorkItem, item_id)
        assert item.state == "ready"
        assert item.title == "Legitimate reopen"
        _item, outcome = mint_or_sync_from_github(
            session,
            "owner/repo",
            _issue(title="Stale sweep", updated_at="2026-09-20T10:02:30Z"),
            actor="github:sweep",
            source_ordered=True,
            source_ref="github:sweep",
        )
        assert outcome == "stale_ignored"
        assert item.title == "Legitimate reopen"


def test_duplicate_and_concurrent_delivery_apply_once(tmp_path, monkeypatch):
    engine, client = _setup(tmp_path, monkeypatch)
    payload = _payload()

    first = _post(client, payload, delivery="same-delivery")
    duplicate = _post(client, payload, delivery="same-delivery")
    assert first.json()["status"] == "accepted"
    assert duplicate.json() == {"status": "duplicate"}

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda _unused: _post(
                    client,
                    _payload(
                        issue=_issue(
                            title="Concurrent", updated_at="2026-09-20T10:01:00Z"
                        )
                    ),
                    delivery="concurrent-delivery",
                ),
                range(2),
            )
        )
    assert sorted(response.json()["status"] for response in responses) == [
        "accepted",
        "duplicate",
    ]
    with Session(engine) as session:
        assert len(session.exec(select(FactoryWebhookDelivery)).all()) == 2
        assert len(session.exec(select(WorkItem)).all()) == 1
        assert len(session.exec(select(WorkItemEvent)).all()) == 2


def test_processing_failure_rolls_back_claim_for_safe_retry(tmp_path, monkeypatch):
    engine, client = _setup(tmp_path, monkeypatch)
    original = webhook._mint_or_sync_from_github_locked

    def fail(*_args, **_kwargs):
        raise RuntimeError("transient database failure")

    monkeypatch.setattr(webhook, "_mint_or_sync_from_github_locked", fail)
    failed = _post(client, _payload(), delivery="retryable-delivery")
    assert failed.status_code == 500
    with Session(engine) as session:
        assert session.get(FactoryWebhookDelivery, "retryable-delivery") is None
        assert session.exec(select(FactoryGithubIssueState)).all() == []
        assert session.exec(select(WorkItem)).all() == []

    monkeypatch.setattr(webhook, "_mint_or_sync_from_github_locked", original)
    retried = _post(client, _payload(), delivery="retryable-delivery")
    assert retried.status_code == 200
    assert retried.json()["outcome"] == "trusted_minted"


def test_dependency_failure_rolls_back_claim_and_work_item(tmp_path, monkeypatch):
    engine, client = _setup(tmp_path, monkeypatch)

    def fail(*_args, **_kwargs):
        raise RuntimeError("transient dependency reconciliation failure")

    monkeypatch.setattr(webhook, "reconcile_body_edges", fail)
    failed = _post(client, _payload(), delivery="dependency-retry")
    assert failed.status_code == 500
    with Session(engine) as session:
        assert session.get(FactoryWebhookDelivery, "dependency-retry") is None
        assert session.exec(select(FactoryGithubIssueState)).all() == []
        assert session.exec(select(WorkItem)).all() == []


def _edge_numbers(session):
    items = {
        item.id: item.github_issue_number
        for item in session.exec(select(WorkItem)).all()
    }
    return {
        (items[edge.from_id], items[edge.to_id])
        for edge in session.exec(select(WorkItemEdge)).all()
    }


@pytest.mark.parametrize("blocked_first", [True, False])
def test_dependencies_reconcile_when_endpoints_arrive_in_either_order(
    tmp_path, monkeypatch, blocked_first
):
    engine, client = _setup(tmp_path, monkeypatch)
    blocked = _issue(number=6257, body="Blocked by #6258")
    blocker = _issue(number=6258, labels=())

    ordered = (blocked, blocker) if blocked_first else (blocker, blocked)
    for index, issue in enumerate(ordered):
        assert (
            _post(
                client,
                _payload(issue=issue),
                delivery=f"endpoint-{index}",
            ).status_code
            == 200
        )

    with Session(engine) as session:
        assert _edge_numbers(session) == {(6258, 6257)}


def test_dependency_edits_replace_and_remove_stale_edges(tmp_path, monkeypatch):
    engine, client = _setup(tmp_path, monkeypatch)
    for number in (6258, 6259):
        response = _post(
            client,
            _payload(issue=_issue(number=number, labels=())),
            delivery=f"endpoint-{number}",
        )
        assert response.status_code == 200

    assert (
        _post(
            client,
            _payload(issue=_issue(body="Blocked by #6258")),
            delivery="dependency-add",
        ).status_code
        == 200
    )
    with Session(engine) as session:
        assert _edge_numbers(session) == {(6258, 6257)}

    assert (
        _post(
            client,
            _payload(
                action="edited",
                issue=_issue(
                    body="Depends on #6259", updated_at="2026-09-20T10:01:00Z"
                ),
            ),
            delivery="dependency-replace",
        ).status_code
        == 200
    )
    with Session(engine) as session:
        assert _edge_numbers(session) == {(6259, 6257)}

    assert (
        _post(
            client,
            _payload(
                action="edited",
                issue=_issue(
                    body="No dependency now", updated_at="2026-09-20T10:02:00Z"
                ),
            ),
            delivery="dependency-remove",
        ).status_code
        == 200
    )
    with Session(engine) as session:
        assert _edge_numbers(session) == set()


def test_local_authority_row_cannot_be_changed_by_github(tmp_path, monkeypatch):
    engine, client = _setup(tmp_path, monkeypatch)
    opened = _post(client, _payload(), delivery="local-open")
    item_id = opened.json()["work_item_id"]
    with Session(engine) as session:
        item = session.get(WorkItem, item_id)
        item.authority = "local"
        session.add(item)
        session.commit()

    changed = _post(
        client,
        _payload(
            action="edited",
            issue=_issue(title="GitHub replacement", updated_at="2026-09-20T10:01:00Z"),
        ),
        delivery="local-edit",
    )
    assert changed.json()["outcome"] == "local_untouched"
    with Session(engine) as session:
        assert session.get(WorkItem, item_id).title == "Webhook item"


def test_concurrent_local_handoff_prevents_webhook_close(tmp_path, monkeypatch):
    engine, client = _setup(tmp_path, monkeypatch)
    opened = _post(client, _payload(), delivery="handoff-open")
    item_id = opened.json()["work_item_id"]
    handoff_flushed = ThreadEvent()
    release_handoff = ThreadEvent()
    claim_started = ThreadEvent()
    original_claim = webhook._claim_delivery

    def observed_claim(*args, **kwargs):
        claim_started.set()
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(webhook, "_claim_delivery", observed_claim)

    def handoff():
        with Session(engine) as session:
            set_authority_local(
                session,
                item_id,
                actor="operator:test",
                author_kind="operator",
                cause_kind="test",
                cause_ref="concurrent-handoff",
                stated_reason="operator accepted ownership",
            )
            session.flush()
            handoff_flushed.set()
            assert release_handoff.wait(timeout=5)
            session.commit()

    closed_issue = _issue(updated_at="2026-09-20T10:01:00Z")
    closed_issue["state"] = "closed"
    with ThreadPoolExecutor(max_workers=2) as pool:
        handoff_future = pool.submit(handoff)
        assert handoff_flushed.wait(timeout=5)
        webhook_future = pool.submit(
            _post,
            client,
            _payload(action="closed", issue=closed_issue),
            delivery="concurrent-close",
        )
        assert claim_started.wait(timeout=5)
        release_handoff.set()
        handoff_future.result(timeout=5)
        response = webhook_future.result(timeout=5)

    assert response.json()["outcome"] == "local_untouched"
    with Session(engine) as session:
        item = session.get(WorkItem, item_id)
        assert item.authority == "local"
        assert item.state == "ready"
        assert item.close_reason is None
