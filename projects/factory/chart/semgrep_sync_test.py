import json
import os
from pathlib import Path
import runpy
from urllib.parse import parse_qs, urlsplit

import pytest

sync = runpy.run_path(
    os.getenv("SYNC_SCRIPT", Path(__file__).parent / "files/semgrep_sync.py")
)["sync"]


def row(number, **values):
    return {
        "id": number,
        "ref": "refs/heads/main",
        "repository": {"name": "jomcgi-org/homelab"},
        "location": {"file_path": "src/file", "line": 12},
        "rule_name": "untrusted\n```",
        "status": "open",
        "severity": "high",
        **values,
    }


class Board:
    def __init__(self):
        self.issues = []
        self.writes = []

    def __call__(self, path, method="GET", body=None):
        if method == "POST":
            assert path == "/projects/INTSEC/issues"
            self.issues.append({**body, "id": len(self.issues) + 1})
        elif method == "PATCH":
            assert path.endswith("/factory-semgrep")
            assert "finding" in body
            self.issues[int(path.split("/")[2]) - 1].update(body)
        elif path == "/me":
            return {
                "authenticated": True,
                "user": {"username": "intsec-sync", "isAgent": True, "id": "bot"},
            }
        elif path == "/projects/INTSEC":
            return {
                "labels": [{"name": "semgrep", "id": 5}],
                "columns": [
                    {"id": 1, "stateType": "backlog"},
                    {"id": 2, "stateType": "completed"},
                ],
            }
        elif path.endswith("/archived"):
            return []
        elif path.endswith("/board"):
            return {"issues": self.issues}
        else:
            return {
                "items": [
                    {
                        "action": "created",
                        "actorUserId": "human"
                        if path.startswith("/issues/1/")
                        else "bot",
                    }
                ]
            }
        self.writes.append((method, path))


def test_mirror():
    board = Board()
    board.issues.append(
        {"id": 1, "description": "Semgrep finding ID: sast/1\n", "labelIds": [5]}
    )
    data = {
        "sast": [row(1), row(2, severity="low"), row(3, ref="refs/pull/7/merge")],
        "sca": [row(4, exposure="reachable"), row(5, exposure="unreachable")],
        "secrets": [row(6, severity="low")],
    }

    def get(path):
        product = parse_qs(urlsplit(path).query).get("issue_type", ["secrets"])[0]
        return {"findings": data[product]}

    sync(get, "org", board)
    assert [i["title"] for i in board.issues[1:]] == [
        "Semgrep sast/1",
        "Semgrep sca/4",
        "Semgrep secrets/6",
    ]
    sync(get, "org", board)
    assert len(board.writes) == 3
    data["sast"][0]["location"]["line"] = 42
    sync(get, "org", board)
    issue = board.issues[1]
    assert "src/file:42" in issue["description"]
    assert json.dumps("untrusted\n```") in issue["description"]
    for status, col in [("fixed", 2), ("open", 1), ("ignored", 2), ("open", 1)]:
        data["sast"][0]["status"] = status
        sync(get, "org", board)
        assert issue["columnId"] == col
    assert "title" not in board.issues[0]


def test_failure_on_last_product_writes_nothing():
    def get(path):
        if path.startswith("/secrets"):
            raise TimeoutError()
        return {"findings": [row(1)]}

    with pytest.raises(TimeoutError):
        sync(
            get,
            "org",
            lambda *args: pytest.fail("INTSEC accessed before snapshot completed"),
        )
