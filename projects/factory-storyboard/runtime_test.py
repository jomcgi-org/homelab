from datetime import datetime, timezone

import hashlib
import json
from pathlib import Path
from jsonschema import Draft7Validator

import runtime
from task_reader import approved_messages


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

    def task(task_id):
        assert task_id == "task"
        return approved_messages(
            {
                "history": [
                    {
                        "role": "ROLE_USER",
                        "parts": [{"text": "Private revision goal reasons"}],
                    },
                    {
                        "role": "ROLE_AGENT",
                        "parts": [
                            {"text": "Unapproved reply with private goal reasons"}
                        ],
                    },
                    {
                        "role": "ROLE_AGENT",
                        "parts": [{"text": "Update from private-user"}],
                    },
                ]
            },
            [hashlib.sha256(b"Update from private-user").hexdigest()],
        )

    original_turn = runtime.public_turn
    monkeypatch.setattr(runtime, "session_tasks", references)
    monkeypatch.setattr(runtime, "confirmed_messages", task)
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
    assert "Private revision goal reasons" not in json.dumps(result)
    assert "Unapproved reply" not in json.dumps(result)
    assert queried == [4]
    queried.clear()
    monkeypatch.setattr(runtime, "session_tasks", lambda *args: [])
    assert runtime.documents(None, "HOME", project, board, archive) == result
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


def test_task_reader_checks_identity_and_limits_upstream_reads(monkeypatch):
    from http.server import ThreadingHTTPServer
    from threading import Thread
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    import pytest
    import task_reader

    text = "Approved update"
    digest = hashlib.sha256(text.encode()).hexdigest()
    grant = dict(
        namespace="factory",
        agent="coder",
        session="session",
        task="task",
        share="owner-share",
        approvedHashes=[digest],
    )
    address = hashlib.sha256(b"task").hexdigest()
    calls = []

    def completed(*args):
        calls.append(args)
        return {
            "history": [
                {"role": "ROLE_AGENT", "parts": [{"text": text}]},
                {"role": "ROLE_USER", "parts": [{"text": "Private goal reasons"}]},
            ]
        }

    monkeypatch.setenv("UPSTREAM_BEARER", "upstream")
    monkeypatch.setattr(task_reader, "completed_task", completed)
    with ThreadingHTTPServer(("127.0.0.1", 0), task_reader.Handler) as server:
        server.authorization = "Bearer reader"
        server.grants = {address: grant}
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:

            def read(path, bearer, method="GET"):
                req = Request(
                    f"http://127.0.0.1:{server.server_port}{path}",
                    method=method,
                    headers={"Authorization": bearer},
                )
                with urlopen(req, timeout=5) as response:
                    return json.load(response)

            path = "/tasks/" + address
            for target, token, method, expected in [
                (path, "Bearer other", "GET", 403),
                ("/tasks/" + "0" * 64, "Bearer reader", "GET", 404),
                (path, "Bearer reader", "POST", 501),
            ]:
                with pytest.raises(HTTPError) as exc:
                    read(target, token, method)
                assert exc.value.code == expected
            assert not calls
            assert read(path, "Bearer reader") == [digest]
            assert calls == [
                (
                    "http://kagent-controller.kagent.svc:8083/agents/factory/coder",
                    "session",
                    "task",
                    "upstream",
                    "owner-share",
                )
            ]
        finally:
            server.shutdown()
            thread.join()
