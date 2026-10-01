from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from grants import ACTIONS, ActionGrant, check, origin
from session import SessionOwner

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
OWNER = SessionOwner("task", "run", "principal")
GRANT = ActionGrant(
    "task",
    "principal",
    ("https://preview.example.test:443",),
    ACTIONS,
    NOW + timedelta(minutes=5),
)


@pytest.mark.parametrize("action", sorted(ACTIONS))
def test_explicit_execution_action(action):
    assert check(
        GRANT, OWNER, action, "https://preview.example.test/path", now=NOW
    ).allowed


def test_empty_grant_denies():
    grant = ActionGrant("task", "principal")
    assert not check(
        grant, OWNER, "navigate", "https://preview.example.test", now=NOW
    ).allowed


@pytest.mark.parametrize(
    "url",
    (
        None,
        "",
        [],
        1,
        "javascript:alert(1)",
        "file:///tmp/a",
        "ftp://example.test",
        "https://user@preview.example.test",
        "https://user:pass@preview.example.test",
        "http://169.254.169.254/latest/meta-data",
        "http://[::ffff:169.254.169.254]",
        "http://[fe80::1]",
        "http://2852039166",
        "http://0xa9fea9fe",
        "http://169.254.43518",
        "http://0251.0376.0251.0376",
        "https://preview.example.test:0",
        "https://preview.example.test:65536",
        "https://preview.example.test:",
        "https://preview.example.test./",
        "https://preview.example.test\\@evil.test",
        "https://preview.example.test\n",
        "https://pre%76iew.example.test",
    ),
)
def test_bad_urls_refused(url):
    with pytest.raises(ValueError):
        origin(url)
    assert not check(GRANT, OWNER, "navigate", url, now=NOW).allowed


@pytest.mark.parametrize(
    "url",
    (
        "http://preview.example.test",
        "https://preview.example.test:444",
        "https://other.example.test",
        "https://preview.example.test.evil.test",
    ),
)
def test_exact_origin_required(url):
    assert not check(GRANT, OWNER, "navigate", url, now=NOW).allowed


@pytest.mark.parametrize(
    "owner",
    (None, {}, replace(OWNER, task_id="other"), replace(OWNER, principal="other")),
)
def test_wrong_owner(owner):
    assert not check(
        GRANT, owner, "click", "https://preview.example.test", now=NOW
    ).allowed


@pytest.mark.parametrize("action", (None, "", [], 1, "download"))
def test_unlisted_action(action):
    assert not check(
        GRANT, OWNER, action, "https://preview.example.test", now=NOW
    ).allowed


@pytest.mark.parametrize(
    "now",
    (
        NOW + timedelta(minutes=5),
        NOW + timedelta(days=1),
        NOW.replace(tzinfo=None),
        "",
        float("nan"),
    ),
)
def test_expiry_and_invalid_time(now):
    assert not check(
        GRANT, OWNER, "navigate", "https://preview.example.test", now=now
    ).allowed


@pytest.mark.parametrize("grant", (None, {}, "", 1))
def test_missing_grant(grant):
    assert not check(
        grant, OWNER, "click", "https://preview.example.test", now=NOW
    ).allowed


@pytest.mark.parametrize(
    "changes",
    (
        {"task_id": None},
        {"principal": ""},
        {"allowed_origins": None},
        {"allowed_origins": ("https://preview.example.test:443",) * 2},
        {"allowed_origins": ("http://169.254.169.254:80",)},
        {"allowed_origins": ("https://preview.example.test",)},
        {"allowed_actions": None},
        {"allowed_actions": frozenset(("execute",))},
        {"expires_at": None},
        {"expires_at": float("nan")},
    ),
)
def test_malformed_grants_refused(changes):
    with pytest.raises((ValueError, TypeError)):
        replace(GRANT, **changes)


def test_origin_boundaries():
    assert origin("http://example.test:1") == "http://example.test:1"
    assert origin("https://example.test:65535/a") == "https://example.test:65535"
    assert origin("https://[::1]/a") == "https://[::1]:443"
    assert origin("HTTPS://EXAMPLE.TEST/a") == "https://example.test:443"
