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
        return {k: base64.b64decode(v).decode() for k, v in self.secret["data"].items()}

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


class CoordinatorAPI:
    """The pinned roles/member API and effective access under the agent key."""

    def __init__(self, script):
        self.script = script
        self.agent = {
            "id": 8,
            "userId": "coordinator-user",
            "username": "factory-coordinator",
            "projects": [{"id": 9}],
        }
        self.key = "test-private-coordinator-key"
        self.secret = {"data": {"apiKey": base64.b64encode(self.key.encode()).decode()}}
        self.permissions = {
            "work_items": {"read": True, "edit": True},
            "ai_agents": {"read": True},
        }
        self.roles = []
        self.writes = []
        self.effective_reads = 0
        self.fail = None
        self.wrong_identity = False

    def request(self, method, url, body=None, headers=None, ctx=None):
        path = urlsplit(url).path
        response_headers = Message()
        if method != "GET":
            self.writes.append((method, path, copy.deepcopy(body)))
        if path.startswith("/api/v1/namespaces/"):
            assert path.endswith("/factory-itsaplan-agent-factory-coordinator")
            if method == "PUT":
                self.secret = copy.deepcopy(body)
            return 200, response_headers, copy.deepcopy(self.secret)
        if (headers or {}).get("x-api-key"):
            assert headers["x-api-key"] == self.key and "Cookie" not in headers
            assert method == "GET"
            if path == "/me":
                data = {
                    "authenticated": True,
                    "user": {
                        "id": "wrong-user"
                        if self.wrong_identity
                        else self.agent["userId"]
                    },
                }
            else:
                assert path == _PROJECT
                self.effective_reads += 1
                if self.fail == "read":
                    raise self.script.HTTPError(method, url, 403, self.key)
                data = {"permissions": copy.deepcopy(self.permissions)}
            return 200, response_headers, data
        assert headers["Cookie"] == "session=admin"
        data = None
        if path == "/teams/7/ai-agents":
            assert method == "GET"
            data = [self.agent]
        elif path == "/teams/7/roles/options":
            assert method == "GET"
            data = self.roles
        elif path == "/teams/7/roles":
            assert method == "POST"
            role = {"id": 20, "system": False, **copy.deepcopy(body)}
            self.roles.append(role)
            if self.fail == "create response":
                raise OSError("lost role create response")
            data = role
        elif path == "/teams/7/roles/20":
            assert method == "PATCH"
            self.roles[0].update(copy.deepcopy(body))
            data = self.roles[0]
        elif path == _PROJECT + "/members/coordinator-user":
            assert method == "PATCH" and body == {"role": "member", "roleId": 20}
            if self.fail != "grant ignored":
                self.permissions = copy.deepcopy(self.roles[0]["permissions"])
            if self.fail == "grant response":
                raise OSError("lost membership update response")
        else:
            pytest.fail(f"unexpected coordinator API call: {method} {path}")
        return 200, response_headers, copy.deepcopy(data)


@pytest.fixture
def coordinator(bootstrap, monkeypatch):
    script, _, _ = bootstrap
    script.CONFIG.update(
        {
            "coordinator": "factory-coordinator",
            "agents": [
                {"username": "factory-coordinator", "name": "Factory coordinator"}
            ],
        }
    )
    monkeypatch.setenv("AGENT_SECRET_PREFIX", "factory-itsaplan-agent-")
    api = CoordinatorAPI(script)
    monkeypatch.setattr(script, "request", api.request)
    admin = script.Session()
    admin.cookies = {"session": "admin"}
    return script, api, lambda: script.ensure_agents(admin, 7, 9)


def test_coordinator_bootstrap_grants_and_verifies_effective_permissions(
    coordinator, capsys
):
    script, api, run = coordinator
    run()
    assert script.has_coordinator_permissions(api.permissions)
    assert api.effective_reads == 2
    assert len(api.roles) == 1 and not api.roles[0]["system"]
    assert api.roles[0]["permissions"] == script.COORDINATOR_PERMISSIONS
    assert api.roles[0]["permissions"]["ai_agents"] == {"read": True}
    api.writes.clear()
    run()
    assert api.effective_reads == 3 and api.writes == []
    output = capsys.readouterr().out
    assert "verified factory-coordinator on HOME" in output and api.key not in output


@pytest.mark.parametrize("failure", ["create response", "grant response"])
def test_coordinator_bootstrap_recovers_partial_grant_without_duplicate_role(
    coordinator, failure
):
    _, api, run = coordinator
    api.fail = failure
    with pytest.raises(OSError, match="lost"):
        run()
    api.fail = None
    run()
    assert len(api.roles) == 1 and api.permissions["work_items"]["create"] is True


def test_coordinator_bootstrap_repairs_dedicated_role_without_editing_default(
    coordinator,
):
    script, api, run = coordinator
    run()
    api.roles[0]["permissions"]["work_items"]["create"] = False
    api.permissions = copy.deepcopy(api.roles[0]["permissions"])
    api.writes.clear()
    run()
    assert script.has_coordinator_permissions(api.permissions)
    role_writes = [p for m, p, _ in api.writes if p.startswith("/teams/7/roles")]
    assert role_writes == ["/teams/7/roles/20"]


@pytest.mark.parametrize(
    "failure", ["wrong identity", "read", "grant ignored", "system role"]
)
def test_coordinator_bootstrap_fails_closed_without_effective_grant(
    coordinator, failure, capsys
):
    script, api, run = coordinator
    if failure == "wrong identity":
        api.wrong_identity = True
    elif failure == "system role":
        api.roles = [
            {
                "id": 1,
                "name": "Factory coordinator (factory-coordinator)",
                "system": True,
            }
        ]
    else:
        api.fail = failure
    with pytest.raises((SystemExit, script.HTTPError)):
        run()
    output = capsys.readouterr().out
    assert "verified factory-coordinator" not in output and api.key not in output
    if failure != "grant ignored":
        assert not any("/roles" in p or "/members/" in p for _, p, _ in api.writes)


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
