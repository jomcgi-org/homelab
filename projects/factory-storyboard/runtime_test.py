from datetime import datetime, timezone

import hashlib
import json
import logging
from pathlib import Path
import sys
import types
import pytest
from jsonschema import Draft7Validator

import runtime

FINISHED = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
LABELS = [
    {"id": 7, "name": "Private"},
    {"id": 8, "name": "security"},
    {"id": 9, "name": "INTSEC"},
    {"id": 10, "name": "deferred"},
]


def public_id(session):
    return "session-" + hashlib.sha256(session.encode()).hexdigest()


def no_secrets(bodies):
    return set()


def secret_scan(bodies):
    return {i for i, body in enumerate(bodies) if b"SECRET" in body}


def validate(docs):
    schema = json.loads(
        (Path(__file__).parent / "schema/v1/snapshot.schema.json").read_text()
    )
    story, goals, issues, sessions = docs
    validator = Draft7Validator(schema)
    for doc in [story, *goals.values(), *issues.values(), *sessions.values()]:
        validator.validate(doc)


class Store:
    """The connector views: delivery facts and the text of eligible messages.

    The store test checks that storyboard_prose_messages holds completed HOME
    tasks that the binding's own agent ran, never the security agent's.
    """

    def __init__(self, monkeypatch, messages, merged_at=None):
        self.queried = []
        self.messages = messages
        self.merged_at = merged_at
        monkeypatch.setattr(runtime, "delivery", self.delivery)
        monkeypatch.setattr(runtime, "session_messages", self.session_messages)
        monkeypatch.setattr(runtime, "messages_view_ready", lambda cursor: True)

    def delivery(self, cursor, project, issue_id):
        self.queried.append(("delivery", issue_id))
        return dict(
            issue_key=f"HOME-{issue_id}",
            state="done" if self.merged_at else "review",
            merged_at=self.merged_at,
            pr_number=10,
            review_rounds=0,
        )

    def session_messages(self, cursor, project, issue_id):
        assert project == "HOME"
        self.queried.append(("messages", issue_id))
        return [
            dict(session_id=session, task_id=task, finished_at=at, text=text)
            for issue, session, task, at, text in self.messages
            if issue == issue_id
        ]


def board(*issues):
    return [
        dict(
            id=i,
            identifier=f"HOME-{i}",
            projectId=project,
            labelIds=labels,
            title="private-user shipped",
            description="Asked by a@test.dev",
            createdAt="2026-11-02T00:00:00Z",
        )
        for i, project, labels in issues
    ]


PROJECT = {
    "project": {"id": 1},
    "labels": LABELS,
    "assignees": [{"userId": "private-user"}],
}


def test_private_and_security_issues_never_reach_the_connector(monkeypatch):
    store = Store(monkeypatch, [(i, "s", "t", FINISHED, "Reply") for i in range(2, 7)])
    items = board(
        (2, 2, []),  # another project
        (3, 1, [7]),  # Private
        (4, 1, [10]),  # deferred only: public
        (5, 1, [8]),  # security
        (6, 1, [10, 9]),  # INTSEC
    )
    _, _, issues, sessions = runtime.documents(None, "HOME", PROJECT, items, no_secrets)
    assert store.queried == [("delivery", 4), ("messages", 4)]
    assert list(issues) == ["HOME-4"]
    assert [s["issueKey"] for s in sessions.values()] == ["HOME-4"]


def test_normal_session_publishes_by_default(monkeypatch):
    later = datetime(2026, 11, 2, 14, tzinfo=timezone.utc)
    Store(
        monkeypatch,
        [
            (4, "s", "code", FINISHED, "Update from private-user"),
            (4, "s", "code", FINISHED, "Opened the PR"),
            (4, "s", "revise", later, "Fixed the review finding"),
        ],
        merged_at=datetime(2026, 11, 3, tzinfo=timezone.utc),
    )
    docs = runtime.documents(None, "HOME", PROJECT, board((4, 1, [])), no_secrets)
    story, goals, issues, sessions = docs
    (session,) = sessions.values()
    assert session["id"] == public_id("s")
    assert session["title"] == "[redacted] shipped"
    assert session["turns"] == [
        dict(
            number=1,
            at="2026-11-02T12:00:00.000Z",
            role="assistant",
            withheld=False,
            text="Update from [redacted]",
            toolCalls=[],
        ),
        dict(
            number=2,
            at="2026-11-02T12:00:00.000Z",
            role="assistant",
            withheld=False,
            text="Opened the PR",
            toolCalls=[],
        ),
        dict(
            number=3,
            at="2026-11-02T14:00:00.000Z",
            role="assistant",
            withheld=False,
            text="Fixed the review finding",
            toolCalls=[],
        ),
    ]
    issue = issues["HOME-4"]
    assert issue["title"] == "[redacted] shipped"
    assert issue["description"] == "Asked by [redacted]"
    assert [(e["kind"], e.get("turn")) for e in issue["timeline"]] == [
        ("opened", None),
        ("session", 1),
        ("session", 2),
        ("session", 3),
        ("merged", None),
    ]
    assert goals["home"]["issueKeys"] == ["HOME-4"]
    assert goals["home"]["burnUp"] == [
        dict(date="2026-11-02", shipped=0, total=1),
        dict(date="2026-11-03", shipped=1, total=1),
    ]
    assert story["stats"]["mergeHours"]["value"] == 24
    assert story["recentlyShipped"][0]["state"] == "Shipped"
    assert "private-user" not in json.dumps(docs)
    assert "a@test.dev" not in json.dumps(docs)
    validate(docs)


def test_secret_scan_withholds_matching_turns_and_issue_text(monkeypatch, caplog):
    Store(
        monkeypatch,
        [
            (4, "s", "code", FINISHED, "Set the key to SECRET"),
            (4, "s", "code", FINISHED, "Opened the PR"),
        ],
    )
    items = board((4, 1, []))
    items[0]["title"] = "Rotate SECRET"
    with caplog.at_level(logging.WARNING, logger=runtime.__name__):
        docs = runtime.documents(None, "HOME", PROJECT, items, secret_scan)
    _, _, issues, sessions = docs
    assert "SECRET" not in json.dumps(docs)
    (session,) = sessions.values()
    assert session["turns"] == [
        dict(number=1, at="2026-11-02T12:00:00.000Z", role="assistant", withheld=True),
        dict(
            number=2,
            at="2026-11-02T12:00:00.000Z",
            role="assistant",
            withheld=False,
            text="Opened the PR",
            toolCalls=[],
        ),
    ]
    # A matching title falls back to the issue key. The description, which
    # the scan does not match, still publishes.
    assert issues["HOME-4"]["title"] == session["title"] == "HOME-4"
    assert issues["HOME-4"]["description"] == "Asked by [redacted]"
    assert [e["kind"] for e in issues["HOME-4"]["timeline"]] == [
        "opened",
        "session",
        "session",
    ]
    assert caplog.messages == [
        "withheld issue text of HOME-4: secret scan",
        f"withheld turn 1 of {public_id('s')} in HOME-4: secret scan",
    ]
    assert "SECRET" not in caplog.text
    validate(docs)


@pytest.mark.parametrize("label", [7, 8, 9])
def test_issue_stops_publishing_once_private_or_security_labelled(monkeypatch, label):
    store = Store(monkeypatch, [(4, "s", "code", FINISHED, "Coded it")])
    items = board((4, 1, []))
    _, _, issues, sessions = runtime.documents(None, "HOME", PROJECT, items, no_secrets)
    assert list(issues) == ["HOME-4"] and len(sessions) == 1
    # The next cycle reads the new label from itsaplan before any connector read.
    store.queried.clear()
    items[0]["labelIds"] = [label]
    story, goals, issues, sessions = runtime.documents(
        None, "HOME", PROJECT, items, no_secrets
    )
    assert issues == {} and sessions == {}
    assert goals["home"]["issueKeys"] == [] and story["recentlyShipped"] == []
    assert store.queried == []


def test_session_linked_to_two_issues_stops_the_cycle(monkeypatch):
    Store(
        monkeypatch,
        [(4, "s", "a", FINISHED, "First"), (5, "s", "b", FINISHED, "Second")],
    )
    with pytest.raises(ValueError, match="multiple issues"):
        runtime.documents(
            None, "HOME", PROJECT, board((4, 1, []), (5, 1, [])), no_secrets
        )


def test_absent_connector_view_publishes_nothing(monkeypatch, caplog):
    # The storyboard chart can roll out before the connector migration.
    store = Store(monkeypatch, [(4, "s", "t", FINISHED, "Reply")])
    monkeypatch.setattr(runtime, "messages_view_ready", lambda cursor: False)
    with caplog.at_level(logging.ERROR):
        docs = runtime.documents(None, "HOME", PROJECT, board((4, 1, [])), no_secrets)
    assert docs is None
    assert store.queried == []
    assert "storyboard_prose_messages is absent" in caplog.text


@pytest.mark.parametrize("value", [None, ""])
def test_cycle_without_itsaplan_key_skips_cleanly(monkeypatch, caplog, value):
    # The CronJob can run before the factory chart's PostSync bootstrap
    # writes the key. The chart reads it as optional, so the pod starts.
    if value is None:
        monkeypatch.delenv("ITSAPLAN_KEY", raising=False)
    else:
        monkeypatch.setenv("ITSAPLAN_KEY", value)

    def read_board(key):
        raise AssertionError("read the board without a key")

    monkeypatch.setattr(runtime, "read_board", read_board)
    with caplog.at_level(logging.WARNING, logger=runtime.__name__):
        runtime.main()
    assert caplog.messages == ["itsaplan key not bootstrapped yet; skipping this cycle"]


def test_cycle_with_itsaplan_key_reads_the_board(monkeypatch):
    monkeypatch.setenv("ITSAPLAN_KEY", "key")
    monkeypatch.setenv("PUBLIC_PROJECT", "HOME")
    for name in ("boto3", "psycopg", "psycopg.rows"):
        module = types.ModuleType(name)
        module.dict_row = None
        monkeypatch.setitem(sys.modules, name, module)

    class Read(Exception):
        pass

    def read_board(key):
        assert key == "HOME"
        raise Read

    monkeypatch.setattr(runtime, "read_board", read_board)
    with pytest.raises(Read):
        runtime.main()
