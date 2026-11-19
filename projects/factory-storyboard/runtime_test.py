from datetime import datetime, timezone

import hashlib
import json
import logging
from pathlib import Path
import pytest
from jsonschema import Draft7Validator

import runtime


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def test_private_issues_filtered_before_connector_or_session_read(monkeypatch):
    queried = []

    def delivery(cursor, project, issue_id):
        queried.append(issue_id)
        return dict(
            issue_key="HOME-4",
            state="done",
            merged_at=datetime(2026, 11, 3, tzinfo=timezone.utc),
            pr_number=10,
            review_rounds=0,
        )

    monkeypatch.setattr(runtime, "delivery", delivery)

    def references(cursor, project, issue_id):
        assert issue_id == 4
        return [
            dict(
                session_id=session,
                task_id="task",
                kagent_namespace="factory",
                kagent_agent="coder",
            )
            for session in ("shared", "unshared")
        ]

    # The connector view returns digests only, never transcript text.
    journal = [
        ("shared", "Unapproved reply with private goal reasons"),
        ("shared", "Update from private-user"),
        ("unshared", "Another session reply"),
    ]

    def messages(cursor, project, issue_id):
        assert (project, issue_id) == ("HOME", 4)
        return [
            dict(session_id=session, task_id="task", text_sha256=digest(text))
            for session, text in journal
        ]

    original_turn = runtime.public_turn
    monkeypatch.setattr(runtime, "session_tasks", references)
    monkeypatch.setattr(runtime, "task_messages", messages)
    monkeypatch.setattr(
        runtime,
        "public_turn",
        lambda turn, users: original_turn(turn, users, scan=lambda _: False),
    )
    project = {
        "project": {"id": 1},
        "labels": [{"id": 7, "name": "Private"}],
        "assignees": [{"userId": "private-user"}],
    }
    board = [
        dict(
            id=i,
            identifier=f"HOME-{i}",
            projectId=pid,
            labelIds=labels,
            title="private-user shipped",
            createdAt="2026-11-02T00:00:00Z",
        )
        for i, pid, labels in [(2, 2, []), (3, 1, [7]), (4, 1, [])]
    ]
    archive = {
        "HOME-4": {
            "title": "private-user shipped",
            "tasks": [
                {
                    "session": "shared",
                    "task": "task",
                    "at": "2026-11-02T12:00:00Z",
                    "assistantTexts": ["Update from private-user"],
                }
            ],
        }
    }
    result = runtime.documents(None, "HOME", project, board, archive)
    assert "Unapproved reply" not in json.dumps(result)
    assert queried == [4]
    # A digest from another session or a changed message withholds the task.
    journal[1] = ("unshared", "Update from private-user")
    withheld = runtime.documents(None, "HOME", project, board, archive)
    assert withheld[3] == {}
    assert [e["kind"] for e in withheld[2]["HOME-4"]["timeline"]] == [
        "opened",
        "merged",
    ]
    journal[1] = ("shared", "Update from private-user")
    assert runtime.documents(None, "HOME", project, board, archive) == result
    # APPROVED_HISTORY alone publishes nothing: a task that the task view
    # does not hold stays withheld.
    queried.clear()
    monkeypatch.setattr(runtime, "session_tasks", lambda *args: [])
    assert runtime.documents(None, "HOME", project, board, archive)[3] == {}
    story, goals, issues, sessions = result
    (session,) = sessions.values()
    assert session["turns"][0]["text"] == "Update from [redacted]"
    assert queried == [4]
    assert goals["home"]["issueKeys"] == ["HOME-4"]
    assert goals["home"]["burnUp"] == [
        dict(date="2026-11-02", shipped=0, total=1),
        dict(date="2026-11-03", shipped=1, total=1),
    ]
    assert issues["HOME-4"]["title"] == "[redacted] shipped"
    assert story["stats"]["mergeHours"]["value"] == 24
    assert story["recentlyShipped"][0]["state"] == "Shipped"

    assert runtime.documents(None, "HOME", project, board, {})[3] == {}
    board[-1]["labelIds"] = [7]
    assert runtime.documents(None, "HOME", project, board, archive)[3] == {}

    schema = json.loads(
        (Path(__file__).parent / "schema/v1/snapshot.schema.json").read_text()
    )
    validator = Draft7Validator(schema)
    for doc in [story, *goals.values(), *issues.values(), *sessions.values()]:
        validator.validate(doc)


UNCONFIRMED = {
    # A review round completes without a connector journal, so the view has
    # no digest for its text.
    "review round": (
        dict(session="s", task="review", assistantTexts=["Private reply"]),
        "review",
        "the view does not confirm its text",
    ),
    "another session": (
        dict(session="old", task="code", assistantTexts=["Private reply"]),
        "code",
        "the task view does not hold this task",
    ),
    "missing from the view": (
        dict(session="s", task="gone", assistantTexts=["Private reply"]),
        "gone",
        "the task view does not hold this task",
    ),
    "no messages": (
        dict(session="s", task="review", assistantTexts=[]),
        "entry 1",
        "malformed approval",
    ),
    "text not a list": (
        dict(session="s", task="review", assistantTexts="Private reply"),
        "entry 1",
        "malformed approval",
    ),
    "no timezone": (
        dict(
            session="s",
            task="review",
            at="2026-11-02T13:00:00",
            assistantTexts=["Private reply"],
        ),
        "entry 1",
        "malformed approval",
    ),
}


@pytest.mark.parametrize("case", UNCONFIRMED.values(), ids=UNCONFIRMED.keys())
def test_unconfirmed_task_withheld_and_rest_published(monkeypatch, caplog, case):
    record, label, reason = case
    monkeypatch.setattr(
        runtime,
        "delivery",
        lambda *args: dict(
            issue_key="HOME-4",
            state="review",
            merged_at=None,
            pr_number=10,
            review_rounds=1,
        ),
    )
    monkeypatch.setattr(
        runtime,
        "session_tasks",
        lambda *args: [
            dict(
                session_id="s",
                task_id=task,
                kagent_namespace="factory",
                kagent_agent=agent,
            )
            for task, agent in (("code", "coder"), ("review", "reviewer"))
        ],
    )
    # The view confirms only the coder's message.
    monkeypatch.setattr(
        runtime,
        "task_messages",
        lambda *args: [
            dict(session_id="s", task_id="code", text_sha256=digest("Coded it"))
        ],
    )
    original_turn = runtime.public_turn
    monkeypatch.setattr(
        runtime,
        "public_turn",
        lambda turn, users: original_turn(turn, users, scan=lambda _: False),
    )
    project = {"project": {"id": 1}, "labels": [], "assignees": []}
    board = [
        dict(
            id=4,
            identifier="HOME-4",
            projectId=1,
            labelIds=[],
            title="Ship it",
            createdAt="2026-11-02T00:00:00Z",
        )
    ]
    archive = {
        "HOME-4": {
            "tasks": [
                dict(
                    session="s",
                    task="code",
                    at="2026-11-02T12:00:00Z",
                    assistantTexts=["Coded it"],
                ),
                dict(dict(at="2026-11-02T13:00:00Z"), **record),
            ]
        }
    }
    with caplog.at_level(logging.WARNING, logger=runtime.__name__):
        result = runtime.documents(None, "HOME", project, board, archive)
    story, goals, issues, sessions = result
    assert "Private reply" not in json.dumps(result)
    (session,) = sessions.values()
    assert session["id"] == "session-" + digest("s")
    assert [turn["text"] for turn in session["turns"]] == ["Coded it"]
    assert [e["kind"] for e in issues["HOME-4"]["timeline"]] == ["opened", "session"]
    assert [s["issueKey"] for s in story["workInProgress"]] == ["HOME-4"]
    assert goals["home"]["issueKeys"] == ["HOME-4"]
    assert caplog.messages == [f"withheld approved task {label} of HOME-4: {reason}"]
    assert "Private reply" not in caplog.text


def test_released_session_publishes_until_approval_withdrawn(monkeypatch, caplog):
    # The connector store after the owner's approval import. ReleaseSession
    # clears the done work item's session ID, but the completed attempt, its
    # journal and the approvals remain.
    attempts = {("s", "code")}
    journal = {("s", "code"): ["Coded it", "Private reply"]}
    approvals = {("s", "code", digest("Coded it"))}

    # Both views match the session and task in an approval, as the
    # migration does, and never read the work item's current session.
    def session_tasks(cursor, project, issue_id):
        return [
            dict(session_id=sid, task_id=task, kagent_namespace="f", kagent_agent="c")
            for sid, task in sorted({(sid, task) for sid, task, _ in approvals})
            if (sid, task) in attempts
        ]

    def task_messages(cursor, project, issue_id):
        return [
            dict(session_id=sid, task_id=task, text_sha256=sha)
            for sid, task, sha in sorted(approvals)
            if sha in {digest(text) for text in journal.get((sid, task), [])}
        ]

    monkeypatch.setattr(
        runtime,
        "delivery",
        lambda *args: dict(
            issue_key="HOME-4",
            state="done",
            merged_at=datetime(2026, 11, 3, tzinfo=timezone.utc),
            pr_number=10,
            review_rounds=1,
        ),
    )
    monkeypatch.setattr(runtime, "session_tasks", session_tasks)
    monkeypatch.setattr(runtime, "task_messages", task_messages)
    original_turn = runtime.public_turn
    monkeypatch.setattr(
        runtime,
        "public_turn",
        lambda turn, users: original_turn(turn, users, scan=lambda _: False),
    )
    project = {
        "project": {"id": 1},
        "labels": [{"id": 7, "name": "Private"}],
        "assignees": [],
    }
    board = [
        dict(
            id=4,
            identifier="HOME-4",
            projectId=1,
            labelIds=[],
            title="Ship it",
            createdAt="2026-11-02T00:00:00Z",
        )
    ]
    archive = {
        "HOME-4": {
            "tasks": [
                dict(
                    session="s",
                    task="code",
                    at="2026-11-02T12:00:00Z",
                    assistantTexts=["Coded it"],
                )
            ]
        }
    }
    story, goals, issues, sessions = runtime.documents(
        None, "HOME", project, board, archive
    )
    (session,) = sessions.values()
    assert [turn["text"] for turn in session["turns"]] == ["Coded it"]
    assert [e["kind"] for e in issues["HOME-4"]["timeline"]] == [
        "opened",
        "session",
        "merged",
    ]
    assert "Private reply" not in json.dumps([story, goals, issues, sessions])

    # The owner withdraws the approval from the store. APPROVED_HISTORY
    # still holds the task, but the next cycle withholds it.
    approvals.clear()
    with caplog.at_level(logging.WARNING, logger=runtime.__name__):
        withheld = runtime.documents(None, "HOME", project, board, archive)
    assert withheld[3] == {}
    assert [e["kind"] for e in withheld[2]["HOME-4"]["timeline"]] == [
        "opened",
        "merged",
    ]
    assert caplog.messages == [
        "withheld approved task code of HOME-4: the task view does not hold this task"
    ]

    # An issue that loses eligibility publishes nothing, approval or not.
    approvals.add(("s", "code", digest("Coded it")))
    board[0]["labelIds"] = [7]
    _, _, issues, sessions = runtime.documents(None, "HOME", project, board, archive)
    assert issues == {} and sessions == {}
