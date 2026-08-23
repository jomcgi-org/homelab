"""Unit tests for files/reconcile_team_mapping.py against a scripted CF API.

The script is loaded from its chart path (it is shipped inside the chart and
mounted from a ConfigMap, not installed as a package). The fake API records
every call so each test asserts what the job WROTE, not only what it printed.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

SCRIPT = Path(
    os.environ.get(
        "RECONCILE_SCRIPT",
        Path(__file__).parent / "files" / "reconcile_team_mapping.py",
    )
)
spec = importlib.util.spec_from_file_location("reconcile_team_mapping", SCRIPT)
rtm = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(rtm)

ADMIN_ID = "ddfa9777454146908de4944d6786d002"
DECLARED = [
    {
        "authentikGroup": "homelab-admin",
        "team": "homelab-admin",
        "description": "Full monolith catalogue.",
    }
]


class FakeCF:
    """Enough of Context Forge 1.0.7's teams and sso admin API to drive the job."""

    def __init__(
        self, teams: dict[str, str] | None = None, provider: dict | None = None
    ):
        self.teams = dict(teams or {})
        self.provider = provider
        self.calls: list[tuple[str, str, object]] = []
        self.next_id = 100

    def __call__(self, method: str, path: str, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and path.startswith("/teams/"):
            items = [
                {"name": n, "id": i, "is_personal": False}
                for n, i in self.teams.items()
            ]
            items.append(
                {
                    "name": "Platform Administrator's Team",
                    "id": "personal1",
                    "is_personal": True,
                }
            )
            return 200, {"teams": items, "total": len(items)}
        if method == "POST" and path == "/teams/":
            self.next_id += 1
            self.teams[body["name"]] = f"team{self.next_id}"
            return 201, {"id": self.teams[body["name"]], "name": body["name"]}
        if path == "/auth/sso/admin/providers/authentik":
            if self.provider is None:
                return 404, {"detail": "SSO provider 'authentik' not found"}
            if method == "GET":
                return 200, dict(self.provider)
            if method == "PUT":
                # Mirror the real router: None fields dropped, the rest set.
                self.provider.update({k: v for k, v in body.items() if v is not None})
                return 200, {"id": "authentik", "is_enabled": True}
        raise AssertionError(f"unexpected call {method} {path}")

    def writes(self):
        return [(m, p, b) for m, p, b in self.calls if m != "GET"]


def provider_row(mapping):
    return {
        "id": "authentik",
        "client_id": "cid",
        "issuer": "https://auth.example/",
        "api_audience": "cid",
        "trusted_for_api_auth": True,
        "team_mapping": mapping,
    }


def test_in_sync_row_is_not_rewritten():
    cf = FakeCF(
        teams={"homelab-admin": ADMIN_ID},
        provider=provider_row({"homelab-admin": ADMIN_ID}),
    )
    assert rtm.reconcile(cf, DECLARED, log=lambda _: None) == "in-sync"
    # A PUT invalidates CF's identity caches, so a no-op tick must not write.
    assert cf.writes() == []


def test_drifted_mapping_is_put_back_with_only_team_mapping():
    cf = FakeCF(teams={"homelab-admin": ADMIN_ID}, provider=provider_row({}))
    assert rtm.reconcile(cf, DECLARED, log=lambda _: None) == "updated"
    assert cf.writes() == [
        (
            "PUT",
            "/auth/sso/admin/providers/authentik",
            {"team_mapping": {"homelab-admin": ADMIN_ID}},
        )
    ]
    # Every other provider field survives: no CLIENT_ID needed in the chart.
    assert cf.provider["client_id"] == "cid"
    assert cf.provider["team_mapping"] == {"homelab-admin": ADMIN_ID}


def test_missing_team_is_created_and_mapped_by_id():
    cf = FakeCF(teams={}, provider=provider_row({}))
    assert rtm.reconcile(cf, DECLARED, log=lambda _: None) == "updated"
    post = [c for c in cf.writes() if c[0] == "POST"]
    assert post == [
        (
            "POST",
            "/teams/",
            {
                "name": "homelab-admin",
                "description": "Full monolith catalogue.",
                "visibility": "private",
            },
        )
    ]
    assert cf.provider["team_mapping"] == {"homelab-admin": cf.teams["homelab-admin"]}


def test_personal_team_never_matches_a_declared_name():
    # The admin's personal team is listed alongside real teams; a declared team
    # with that name must create a real one rather than map onto it.
    cf = FakeCF(teams={}, provider=provider_row({}))
    declared = [
        {
            "authentikGroup": "g",
            "team": "Platform Administrator's Team",
            "description": "",
        }
    ]
    rtm.reconcile(cf, declared, log=lambda _: None)
    assert cf.provider["team_mapping"] != {"g": "personal1"}


def test_missing_provider_row_fails_loudly_without_creating_one():
    cf = FakeCF(teams={"homelab-admin": ADMIN_ID}, provider=None)
    with pytest.raises(rtm.ReconcileError, match="provision-mcp-auth.sh"):
        rtm.reconcile(cf, DECLARED, log=lambda _: None)
    assert cf.writes() == []


def test_put_that_does_not_stick_is_an_error():
    class StickyCF(FakeCF):
        def __call__(self, method, path, body=None):
            status, payload = super().__call__(method, path, body)
            if method == "PUT":
                self.provider["team_mapping"] = {}  # simulate a partial apply
            return status, payload

    cf = StickyCF(teams={"homelab-admin": ADMIN_ID}, provider=provider_row({}))
    with pytest.raises(rtm.ReconcileError, match="reads back"):
        rtm.reconcile(cf, DECLARED, log=lambda _: None)


def test_api_error_surfaces_status_and_body():
    def api(method, path, body=None):
        return 500, {"detail": "boom"}

    with pytest.raises(rtm.ReconcileError, match="GET /teams/ returned HTTP 500"):
        rtm.reconcile(api, DECLARED, log=lambda _: None)


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        json.dumps([{"team": "x"}]),
        json.dumps(
            [{"authentikGroup": "A", "team": "x"}, {"authentikGroup": "a", "team": "y"}]
        ),
    ],
)
def test_declared_teams_are_validated(raw):
    with pytest.raises(rtm.ReconcileError):
        rtm.parse_declared_teams(raw)


def test_declared_teams_strip_and_default_description():
    out = rtm.parse_declared_teams(
        json.dumps([{"authentikGroup": " homelab-admin ", "team": "homelab-admin"}])
    )
    assert out == [
        {"authentikGroup": "homelab-admin", "team": "homelab-admin", "description": ""}
    ]
