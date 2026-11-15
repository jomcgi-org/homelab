"""Joe's sessions bootstrap against the itsaplan 1.10.0 invite API contract.

Exercise the real cookie and Secret handling with an in-memory API, including
retries after writes succeed but the bootstrap run fails.
"""

from __future__ import annotations

import base64
import copy
from email.message import Message
import importlib.util
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

_SCRIPT = Path(
    os.environ.get(
        "BOOTSTRAP",
        Path(__file__).resolve().parent / "files" / "itsaplan" / "bootstrap.py",
    )
)
_SECRET = "factory-itsaplan-joe-sessions"
_EMAIL = "joe-sessions@jomcgi.dev"
_PROJECT = "/projects/7.HOME"


class API:
    def __init__(self, script):
        self.script = script
        self.secret = {"data": {}}
        self.user = None
        self.password = None
        self.invite = None
        self.member = False
        self.team_member = False
        self.expired = False
        self.fail = None
        self.writes = []
        self.viewer = {"role": "member", "teamRole": "member", "via": "member"}
        self.workspace_role = None
        self.permissions = {"read": True, "create": True, "edit": True}
        self.active_sessions = set()

    def credentials(self):
        return {
            k: base64.b64decode(v).decode() for k, v in self.secret["data"].items()
        }

    def request(self, method, url, body=None, headers=None, ctx=None):
        parsed = urlsplit(url)
        path = parsed.path
        response_headers = Message()
        if method != "GET":
            self.writes.append((method, path))
        if parsed.hostname == "kubernetes":
            assert path == f"/api/v1/namespaces/factory/secrets/{_SECRET}"
            if method == "PUT":
                self.secret = copy.deepcopy(body)
            else:
                assert method == "GET"
            return 200, response_headers, copy.deepcopy(self.secret)

        cookie = (headers or {}).get("Cookie")
        if path.startswith("/api/auth/sign-") and path != "/api/auth/sign-out":
            assert method == "POST"
            if path == "/api/auth/sign-up/email":
                if self.fail == "signup":
                    raise OSError("interrupted before sign-up")
                assert self.invite and not self.user
                assert body["email"] == self.invite["email"] == _EMAIL
                assert body["password"] == self.credentials()["password"]
                assert body["username"] == "joesessions"
                self.password = body["password"]
                self.user = {
                    "id": "sessions-user",
                    "email": _EMAIL,
                    "name": body["name"],
                    "username": body["username"],
                    "role": "user",
                    "isAgent": False,
                }
            else:
                assert path == "/api/auth/sign-in/email"
                if body["password"] != self.password:
                    raise self.script.HTTPError(method, url, 401, "invalid password")
            response_headers.add_header("Set-Cookie", "session=sessions; HttpOnly")
            self.active_sessions.add("session=sessions")
            return 200, response_headers, {"user": self.user}

        assert cookie == "session=admin" or cookie in self.active_sessions
        result = None
        if path == "/api/auth/sign-out":
            self.active_sessions.remove(cookie)
        elif path == "/me":
            assert cookie == "session=sessions"
            result = {"authenticated": True, "user": copy.deepcopy(self.user)}
        elif (
            path in ("/god/users", f"{_PROJECT}/members", "/teams/7/members")
            and method == "GET"
        ):
            assert cookie == "session=admin"
            assert parse_qs(parsed.query)["search"] == [_EMAIL]
            present = self.user and (
                path == "/god/users"
                or (path == f"{_PROJECT}/members" and self.member)
                or (path == "/teams/7/members" and self.team_member)
            )
            items = [copy.deepcopy(self.user)] if present else []
            result = {"items": items, "total": len(items)}
        elif path == f"{_PROJECT}/invites":
            assert cookie == "session=admin"
            if method == "GET":
                result = [copy.deepcopy(self.invite)] if self.invite else []
            else:
                assert method == "POST" and not self.invite
                assert body == {"email": _EMAIL, "role": "member"}
                self.invite = {
                    "id": 5,
                    "token": "private-invite-token",
                    "email": _EMAIL,
                    "role": "member",
                    "teamRole": "member",
                    "roleId": None,
                    "status": "pending",
                }
                result = copy.deepcopy(self.invite)
        elif path == "/invites/private-invite-token":
            assert method == "GET"
            result = {"expired": self.expired}
        elif path == f"{_PROJECT}/invites/5/renew":
            assert method == "POST" and self.expired
            self.expired = False
            result = copy.deepcopy(self.invite)
        elif path == "/invites/private-invite-token/accept":
            assert method == "POST" and cookie == "session=sessions"
            if self.fail == "accept":
                raise OSError("interrupted before acceptance")
            assert not self.member and not self.expired
            self.member = self.team_member = True
            self.invite["status"] = "accepted"
            result = {"projectKey": "HOME", "role": "member"}
        elif path == f"{_PROJECT}/members":
            assert cookie == "session=admin" and method == "POST"
            assert body == {"userId": self.user["id"], "role": "member"}
            assert self.team_member and not self.member
            self.member = True
        elif path == _PROJECT:
            assert method == "GET" and self.member
            result = {
                "viewer": self.viewer,
                "permissions": {"work_items": self.permissions},
            }
        elif path == "/teams/7":
            result = {"workspaceId": 3}
        elif path == "/workspaces":
            result = [{"id": 3, "role": self.workspace_role}]
        elif path == "/api/auth/update-user":
            assert method == "POST" and body == {"name": "Joe (sessions)"}
            self.user.update(body)
        else:
            pytest.fail(f"unexpected API call: {method} {path}")
        return 200, response_headers, result


@pytest.fixture
def bootstrap(tmp_path, monkeypatch):
    (tmp_path / "token").write_text("test-service-account")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"project": {"key": "HOME"}}))
    for key, value in {
        "SA_DIR": str(tmp_path),
        "KUBERNETES_SERVICE_HOST": "kubernetes",
        "KUBERNETES_SERVICE_PORT": "443",
        "NAMESPACE": "factory",
        "API_INTERNAL_URL": "http://itsaplan",
        "APP_URL": "https://factory-plan.jomcgi.dev",
        "CONFIG": str(config),
        "SESSIONS_SECRET": _SECRET,
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("ssl.create_default_context", lambda **_: None)
    spec = importlib.util.spec_from_file_location("bootstrap", _SCRIPT)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    api = API(script)
    monkeypatch.setattr(script, "request", api.request)
    admin = script.Session()
    admin.cookies = {"session": "admin"}
    return script, api, lambda: script.ensure_sessions(admin, 7)


def test_second_run_preserves_member_password_and_secret(bootstrap, capsys):
    _, api, run = bootstrap
    run()
    user = copy.deepcopy(api.user)
    secret = copy.deepcopy(api.secret)
    assert set(api.credentials()) == {"email", "password"}
    assert len(api.password) >= 32
    assert api.user["name"] == "Joe (sessions)"
    assert api.member and api.team_member
    api.writes.clear()
    run()
    assert api.user == user and api.secret == secret
    assert api.writes == [
        ("POST", "/api/auth/sign-in/email"),
        ("POST", "/api/auth/sign-out"),
    ]
    assert not api.active_sessions
    output = capsys.readouterr().out
    assert api.password not in output and "private-invite-token" not in output


@pytest.mark.parametrize("failure", ["signup", "accept"])
def test_retry_completes_invite_without_replacing_password(bootstrap, failure):
    _, api, run = bootstrap
    api.fail = failure
    with pytest.raises(OSError, match="interrupted"):
        run()
    secret = copy.deepcopy(api.secret)
    user = copy.deepcopy(api.user)
    assert not api.active_sessions
    api.fail = None
    api.expired = True
    run()
    assert api.secret == secret and api.member
    if user:
        assert api.user == user
    assert api.writes.count(("POST", f"{_PROJECT}/invites")) == 1
    assert ("POST", f"{_PROJECT}/invites/5/renew") in api.writes
    assert not api.active_sessions


def test_existing_team_member_can_rejoin_project(bootstrap):
    _, api, run = bootstrap
    run()
    api.member = False
    api.writes.clear()
    run()
    assert api.member
    assert ("POST", f"{_PROJECT}/members") in api.writes
    assert ("POST", f"{_PROJECT}/invites") not in api.writes


@pytest.mark.parametrize("missing", ["email", "password"])
def test_existing_account_with_lost_credentials_stops(bootstrap, missing):
    _, api, run = bootstrap
    run()
    del api.secret["data"][missing]
    api.writes.clear()
    with pytest.raises(SystemExit, match="refusing to replace its password"):
        run()
    assert api.writes == []


def test_wrong_password_is_never_rotated(bootstrap):
    script, api, run = bootstrap
    run()
    api.password = "changed-outside-bootstrap"
    secret = copy.deepcopy(api.secret)
    api.writes.clear()
    with pytest.raises(script.HTTPError, match="HTTP 401"):
        run()
    assert api.secret == secret
    assert api.writes == [("POST", "/api/auth/sign-in/email")]


@pytest.mark.parametrize("drift", ["god", "agent", "owner", "workspace", "permissions"])
def test_account_cannot_silently_gain_admin_or_lose_write_access(bootstrap, drift):
    _, api, run = bootstrap
    run()
    if drift == "god":
        api.user["role"] = "god"
    elif drift == "agent":
        api.user["isAgent"] = True
    elif drift == "owner":
        api.viewer["role"] = "owner"
    elif drift == "workspace":
        api.workspace_role = "admin"
    else:
        api.permissions["edit"] = False
    with pytest.raises(SystemExit):
        run()
    assert not api.active_sessions


def test_errors_never_print_response_credentials_or_invite_tokens(bootstrap):
    script, _, _ = bootstrap
    error = script.HTTPError(
        "POST",
        "http://itsaplan/invites/private-invite-token/accept",
        500,
        '{"password":"private-password"}',
    )
    assert "private-" not in str(error)
