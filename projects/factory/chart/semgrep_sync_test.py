import json
import os
from pathlib import Path
import runpy
from urllib.parse import parse_qs, urlsplit

import pytest

module = runpy.run_path(
    os.getenv("SYNC_SCRIPT", Path(__file__).parent / "files/semgrep_sync.py")
)


@pytest.fixture
def sync(tmp_path):
    def run(get, deployment, plan, alert=None):
        def api(path):
            if path == "" and deployment:
                return {"deployments": [{"slug": deployment, "id": 17}]}
            return get(path)

        return module["sync"](
            api, deployment, plan, alert or pytest.fail, tmp_path / "auth-failed"
        )

    return run


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


def secret_row(finding_id, **fields):
    # Wire names from the SecretsService list response, not the Findings API.
    # sdks-io/apimatic-semgrep-python-sdk@aaef3dd9, ProtosSecretsV1SecretsFinding._names.
    return {
        "id": str(finding_id),
        "repository": {"name": "jomcgi-org/homelab"},
        "ref": "refs/heads/main",
        "findingPath": "src/file",
        "ruleHashId": "rule-hash",
        "severity": "SEVERITY_LOW",
        "status": "FINDING_STATUS_OPEN",
        **fields,
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
                "user": {
                    "username": "intsec-sync",
                    "isAgent": True,
                    "id": "bot",
                },
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


def test_mirror(sync):
    board = Board()
    board.issues.append(
        {"id": 1, "description": "Semgrep finding ID: sast/1\n", "labelIds": [5]}
    )
    data = {
        "sast": [row(1), row(2, severity="low"), row(3, ref="refs/pull/7/merge")],
        "sca": [row(4, exposure="reachable"), row(5, exposure="unreachable")],
        "secrets": [secret_row("secret-6"), secret_row(7, ref="feature")],
    }

    alerts = []

    def alert(name, fields):
        alerts.append((name, fields))

    def get(path):
        if parse_qs(urlsplit(path).query).get("page", ["0"]) != ["0"]:
            return {"findings": []}
        product = parse_qs(urlsplit(path).query).get("issue_type", ["secrets"])[0]
        if product == "secrets":
            assert urlsplit(path).path == "/17/secrets"
            return {"findings": data[product]}
        assert urlsplit(path).path == "/org/findings"
        params = parse_qs(urlsplit(path).query)
        assert params["dedup"] == ["true"] and params["page_size"] == ["3000"]
        return {"findings": data[product]}

    sync(get, "org", board, alert)
    assert [i["title"] for i in board.issues[1:]] == [
        "Semgrep sast/1",
        "Semgrep sca/4",
        "Semgrep secrets/secret-6",
    ]
    sync(get, "org", board, alert)
    assert alerts == [
        (
            "SemgrepNewSecret",
            {
                "repository": "jomcgi-org/homelab",
                "file": "src/file",
                "link": "https://semgrep.dev/orgs/org/findings/secret-6",
            },
        )
    ]
    assert len(board.writes) == 3
    secret_issue = board.issues[-1]
    assert "rule-hash" in secret_issue["description"]
    for status, col in [("FINDING_STATUS_FIXED", 2), ("FINDING_STATUS_OPEN", 1)]:
        data["secrets"][0]["status"] = status
        sync(get, "org", board, alert)
        assert secret_issue["columnId"] == col
    assert len(alerts) == 1
    data["sast"][0]["location"]["line"] = 42
    sync(get, "org", board, alert)
    issue = board.issues[1]
    assert "src/file:42" in issue["description"]
    assert json.dumps("untrusted\n```") in issue["description"]
    for status, col in [("fixed", 2), ("open", 1), ("ignored", 2), ("open", 1)]:
        data["sast"][0]["status"] = status
        sync(get, "org", board, alert)
        assert issue["columnId"] == col
    assert "title" not in board.issues[0]


def test_failure_on_last_product_writes_nothing(sync):
    def get(path):
        if path.startswith("/17/secrets"):
            raise TimeoutError()
        return {
            "findings": [row(1)]
            if parse_qs(urlsplit(path).query)["page"] == ["0"]
            else []
        }

    with pytest.raises(TimeoutError):
        sync(
            get,
            "org",
            lambda *args: pytest.fail("INTSEC accessed before snapshot completed"),
        )


def test_linked_finding_does_not_stop_later_findings(sync):
    from urllib.error import HTTPError

    board = Board()
    # Issue 1 is human-created in the fake feed, so the findings start at 2.
    board.issues.append({"id": 1, "description": "", "labelIds": []})
    data = {"sast": [row(1), row(2)], "sca": [], "secrets": []}

    def get(path):
        if parse_qs(urlsplit(path).query).get("page", ["0"]) != ["0"]:
            return {"findings": []}
        product = parse_qs(urlsplit(path).query).get("issue_type", ["secrets"])[0]
        return {"findings": data[product]}

    sync(get, "org", board)
    real = Board.__call__

    def linked(self, path, method="GET", body=None):
        if method == "PATCH" and body.get("finding") == "sast/1":
            raise HTTPError(path, 409, "Finding has a parent or subtasks", {}, None)
        return real(self, path, method, body)

    Board.__call__ = linked
    try:
        for r in data["sast"]:
            r["status"] = "fixed"
        with pytest.raises(module["DeferredFindings"]) as raised:
            sync(get, "org", board)
    finally:
        Board.__call__ = real
    assert raised.value.keys == ["sast/1"]
    assert board.issues[1]["title"] == "Semgrep sast/1"
    assert board.issues[2]["columnId"] == 2


def test_finding_data_has_no_literal_mention(sync):
    board = Board()
    board.issues.append({"id": 1, "description": "", "labelIds": []})
    data = {"sast": [row(1, rule_name="@joe rule")], "sca": [], "secrets": []}

    def get(path):
        if parse_qs(urlsplit(path).query).get("page", ["0"]) != ["0"]:
            return {"findings": []}
        product = parse_qs(urlsplit(path).query).get("issue_type", ["secrets"])[0]
        return {"findings": data[product]}

    sync(get, "org", board)
    description = board.issues[1]["description"]
    assert "@" not in description
    assert "\\u0040joe rule" in description


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("deployment", ["org", ""])
def test_auth_error_alerts_once_before_board_access(status, deployment, sync):
    from urllib.error import HTTPError

    def get(path):
        if path.startswith("/org/findings"):
            return {
                "findings": [row(1)]
                if parse_qs(urlsplit(path).query)["page"] == ["0"]
                else []
            }
        raise HTTPError(path, status, "", {}, None)

    alerts = []
    for _ in range(2):
        sync(
            get,
            deployment,
            lambda *args: pytest.fail("accessed INTSEC"),
            lambda *args: alerts.append(args),
        )
    assert len(alerts) == 1
    sync(lambda _: {"findings": []}, "org", Board())
    sync(get, deployment, pytest.fail, lambda *args: alerts.append(args))
    assert [name for name, _ in alerts] == ["SemgrepAuthenticationFailed"] * 2
    assert alerts[0][1]["outage"] != alerts[1][1]["outage"]


@pytest.mark.parametrize("reject_large", [False, True])
@pytest.mark.parametrize("with_paging", [False, True])
def test_findings_continue_past_server_capped_pages(reject_large, with_paging):
    from urllib.error import HTTPError

    def get(path):
        params = parse_qs(urlsplit(path).query)
        if params.get("issue_type") != ["sast"]:
            return {"findings": []}
        if reject_large and params["page_size"] == ["3000"]:
            raise HTTPError(path, 400, "page_size", {}, None)
        page = int(params["page"][0])
        payload = {"findings": [row(page + 1)] if page < 2 else []}
        if with_paging:
            assert page <= 1
            payload["paging"] = {"next": "/findings?page=1" if page == 0 else None}
        return payload

    assert list(module["snapshot"](get, "org", 17)) == ["sast/1", "sast/2"]


def test_secrets_cursor_ends_on_nonempty_terminal_page():
    calls = []

    def get(path):
        if path.startswith("/org/findings?"):
            return {"findings": []}
        assert urlsplit(path).path == "/17/secrets"
        calls.append(parse_qs(urlsplit(path).query))
        if len(calls) == 1:
            return {"findings": [secret_row(1)], "cursor": "a/b+= c"}
        assert len(calls) == 2
        return {
            "findings": [
                {
                    "id": "2",
                    "ref": "main",
                    "repository": {"name": "jomcgi-org/homelab"},
                    "status": "FINDING_STATUS_OPEN",
                }
            ],
            "cursor": None,
        }

    assert list(module["snapshot"](get, "org", 17)) == ["secrets/1", "secrets/2"]
    assert calls == [{"limit": ["2000"]}, {"limit": ["2000"], "cursor": ["a/b+= c"]}]
