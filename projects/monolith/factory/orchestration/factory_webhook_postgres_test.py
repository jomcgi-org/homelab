"""PostgreSQL lock-order coverage for GitHub issue source snapshots."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, text
from sqlalchemy.pool import NullPool
from sqlmodel import Session, create_engine, select

from factory.orchestration import factory_webhook as webhook
from factory.orchestration import work_items
from factory.orchestration.factory_models import (
    FactoryGithubIssueState,
    FactoryReceipt,
    FactoryWebhookDelivery,
    WorkItem,
    WorkItemEdge,
    WorkItemEvent,
)

WAIT_SECONDS = 5
HOLDER_WAIT_SECONDS = 15


def _issue(case, *, title: str, updated_at: str, state: str = "open") -> dict:
    return {
        "number": case.issue_number,
        "title": title,
        "body": "public issue body",
        "html_url": (
            f"https://github.com/{case.repo}/issues/{case.issue_number}"
        ),
        "state": state,
        "labels": [{"name": "agent-ready"}],
        "user": {"login": "jomcgi", "type": "User"},
        "created_at": "2026-09-20T10:00:00Z",
        "updated_at": updated_at,
    }


def _payload(case, action: str, issue: dict) -> dict:
    return {
        "action": action,
        "issue": issue,
        "repository": {"full_name": case.repo},
        "sender": {"login": "delivery-sender"},
    }


@pytest.fixture
def case(pg, monkeypatch):
    identity = uuid4().hex[:12]
    repo = f"factory-locks/{identity}"
    issue_number = 6_257
    engines = {}

    def application_name(actor: str) -> str:
        return f"factory-webhook-{identity}-{actor}"

    def engine_for(actor: str):
        if actor not in engines:
            engines[actor] = create_engine(
                pg.url,
                poolclass=NullPool,
                connect_args={
                    "application_name": application_name(actor),
                    "connect_timeout": WAIT_SECONDS,
                    "options": "-c lock_timeout=15000 -c statement_timeout=20000",
                },
            )
        return engines[actor]

    monkeypatch.setenv("FACTORY_GITHUB_WEBHOOK_REPOSITORY", repo)
    monkeypatch.setenv("FACTORY_GITHUB_WEBHOOK_TRUSTED_AUTHORS", "jomcgi")
    value = SimpleNamespace(
        repo=repo,
        issue_number=issue_number,
        engine_for=engine_for,
        application_name=application_name,
    )
    try:
        yield value
    finally:
        with Session(engine_for("cleanup")) as session:
            item_ids = list(
                session.exec(
                    select(WorkItem.id).where(WorkItem.github_repo == repo)
                ).all()
            )
            if item_ids:
                session.exec(
                    delete(WorkItemEdge).where(
                        (WorkItemEdge.from_id.in_(item_ids))
                        | (WorkItemEdge.to_id.in_(item_ids))
                    )
                )
                session.exec(
                    delete(WorkItemEvent).where(
                        WorkItemEvent.work_item_id.in_(item_ids)
                    )
                )
            session.exec(
                delete(FactoryWebhookDelivery).where(
                    FactoryWebhookDelivery.repo == repo
                )
            )
            session.exec(
                delete(FactoryGithubIssueState).where(
                    FactoryGithubIssueState.repo == repo
                )
            )
            session.exec(delete(FactoryReceipt).where(FactoryReceipt.repo == repo))
            session.exec(delete(WorkItem).where(WorkItem.github_repo == repo))
            session.commit()
        for engine in engines.values():
            engine.dispose()


def _hold_after_source_fence(monkeypatch, source_ref: str):
    locked = Queue(maxsize=1)
    proceed = Event()
    original = work_items.order_github_issue_snapshot
    held = Event()

    def observed(session, repo, issue, *, source_ref: str):
        result = original(session, repo, issue, source_ref=source_ref)
        if source_ref == target and result[1] is None and not held.is_set():
            held.set()
            locked.put(
                session.execute(text("SELECT pg_backend_pid()")).scalar_one(),
                timeout=WAIT_SECONDS,
            )
            assert proceed.wait(HOLDER_WAIT_SECONDS), "source fence was not released"
        return result

    target = source_ref
    monkeypatch.setattr(work_items, "order_github_issue_snapshot", observed)
    monkeypatch.setattr(webhook, "order_github_issue_snapshot", observed)
    return locked, proceed


def _wait_for_blocked_connection(case, actor: str, blocker_pid: int) -> None:
    deadline = monotonic() + WAIT_SECONDS
    poll = Event()
    while monotonic() < deadline:
        with case.engine_for("observer").connect() as connection:
            blocked_pid = connection.execute(
                text("""
                    SELECT pid FROM pg_stat_activity
                     WHERE application_name = :actor
                       AND wait_event_type = 'Lock'
                       AND :blocker = ANY(pg_blocking_pids(pid))
                """),
                {
                    "actor": case.application_name(actor),
                    "blocker": blocker_pid,
                },
            ).scalar_one_or_none()
        if blocked_pid is not None:
            assert blocked_pid != blocker_pid
            return
        poll.wait(0.01)
    pytest.fail(f"{actor} did not wait on PostgreSQL backend {blocker_pid}")


def _deliver(case, actor: str, delivery_id: str, action: str, issue: dict) -> dict:
    with Session(case.engine_for(actor)) as session:
        return webhook.process_delivery(
            session,
            delivery_id=delivery_id,
            event="issues",
            payload=_payload(case, action, issue),
        )


def _sweep_one(case, actor: str, issue: dict) -> tuple[int | None, str]:
    with Session(case.engine_for(actor)) as session:
        item, outcome = work_items.mint_or_sync_from_github(
            session,
            case.repo,
            issue,
            actor="github:sweep",
            source_ordered=True,
            source_ref="github:sweep",
        )
        session.commit()
        return item.id if item is not None else None, outcome


def test_concurrent_first_open_then_newer_close_rechecks_item_after_fence(
    case, monkeypatch
):
    locked, proceed = _hold_after_source_fence(monkeypatch, "delivery:older-open")
    older_open = _issue(
        case, title="Older open", updated_at="2026-09-20T10:01:00Z"
    )
    newer_close = _issue(
        case,
        title="Newer close",
        updated_at="2026-09-20T10:02:00Z",
        state="closed",
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            _deliver, case, "older", "older-open", "opened", older_open
        )
        try:
            blocker_pid = locked.get(timeout=WAIT_SECONDS)
            second = pool.submit(
                _deliver, case, "newer", "newer-close", "closed", newer_close
            )
            _wait_for_blocked_connection(case, "newer", blocker_pid)
        finally:
            proceed.set()
        assert first.result(timeout=WAIT_SECONDS)["outcome"] == "trusted_minted"
        assert second.result(timeout=WAIT_SECONDS)["outcome"] == "trusted_closed"

    with Session(case.engine_for("verify")) as session:
        item = session.exec(
            select(WorkItem).where(WorkItem.github_repo == case.repo)
        ).one()
        assert item.state == "closed"
        assert item.close_reason == "github_closed"


def test_reversed_arrival_newer_close_prevents_waiting_stale_first_open(
    case, monkeypatch
):
    locked, proceed = _hold_after_source_fence(monkeypatch, "delivery:newer-close")
    newer_close = _issue(
        case,
        title="Newer close",
        updated_at="2026-09-20T10:02:00Z",
        state="closed",
    )
    older_open = _issue(
        case, title="Older open", updated_at="2026-09-20T10:01:00Z"
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            _deliver, case, "newer", "newer-close", "closed", newer_close
        )
        try:
            blocker_pid = locked.get(timeout=WAIT_SECONDS)
            second = pool.submit(
                _deliver, case, "older", "older-open", "opened", older_open
            )
            _wait_for_blocked_connection(case, "older", blocker_pid)
        finally:
            proceed.set()
        assert first.result(timeout=WAIT_SECONDS)["outcome"] == "trusted_closed"
        assert (
            second.result(timeout=WAIT_SECONDS)["outcome"]
            == "trusted_stale_ignored"
        )

    with Session(case.engine_for("verify")) as session:
        assert (
            session.exec(
                select(WorkItem).where(WorkItem.github_repo == case.repo)
            ).all()
            == []
        )


def test_sweep_and_webhook_first_creation_share_source_fence(case, monkeypatch):
    locked, proceed = _hold_after_source_fence(monkeypatch, "github:sweep")
    sweep_open = _issue(
        case, title="Sweep snapshot", updated_at="2026-09-20T10:01:00Z"
    )
    webhook_open = _issue(
        case, title="Webhook snapshot", updated_at="2026-09-20T10:02:00Z"
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_sweep_one, case, "sweep", sweep_open)
        try:
            blocker_pid = locked.get(timeout=WAIT_SECONDS)
            second = pool.submit(
                _deliver,
                case,
                "webhook",
                "newer-webhook",
                "opened",
                webhook_open,
            )
            _wait_for_blocked_connection(case, "webhook", blocker_pid)
        finally:
            proceed.set()
        _item_id, sweep_outcome = first.result(timeout=WAIT_SECONDS)
        assert sweep_outcome == "minted"
        assert second.result(timeout=WAIT_SECONDS)["outcome"] == "trusted_synced"

    with Session(case.engine_for("verify")) as session:
        items = session.exec(
            select(WorkItem).where(WorkItem.github_repo == case.repo)
        ).all()
        assert len(items) == 1
        assert items[0].title == "Webhook snapshot"
        source = session.get(
            FactoryGithubIssueState, (case.repo, case.issue_number)
        )
        assert source.source_ref == "delivery:newer-webhook"
