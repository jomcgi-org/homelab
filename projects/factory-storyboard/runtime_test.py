from datetime import datetime, timezone

import json
from pathlib import Path
from jsonschema import Draft7Validator

import runtime


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

    def task(url, session, task_id, bearer, share):
        assert (session, bearer, share) == ("shared", "visitor", "share")
        return {
            "status": {"timestamp": "2026-11-02T12:00:00Z"},
            "history": [
                {"role": "ROLE_AGENT", "parts": [{"text": "Update from private-user"}]}
            ],
        }

    original_turn = runtime.public_turn
    monkeypatch.setattr(runtime, "session_tasks", references)
    monkeypatch.setattr(runtime, "completed_task", task)
    monkeypatch.setattr(
        runtime,
        "public_turn",
        lambda turn, users: original_turn(turn, users, scan=lambda _: False),
    )
    monkeypatch.setenv("KAGENT_URL", "http://kagent.test")
    monkeypatch.setenv("KAGENT_BEARER", "visitor")
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
    story, goals, issues, sessions = runtime.documents(
        None, "HOME", project, board, {"shared": "share"}
    )
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

    schema = json.loads(
        (Path(__file__).parent / "schema/v1/snapshot.schema.json").read_text()
    )
    validator = Draft7Validator(schema)
    for doc in [story, *goals.values(), *issues.values(), *sessions.values()]:
        validator.validate(doc)
