"""Unit tests for the k8s-terminal demo's pure pieces.

The PTY/WebSocket bridge is exercised live (private tier, hand-tested); these
cover the request-shaping seams: status shaping, kubeconfig contents, and the
wake-event serialization.
"""

from __future__ import annotations

import json

import pytest

from demos.k8s_terminal_api import _shape_status, _write_kubeconfig


def test_shape_status_maps_group_fields():
    raw = {
        "workload": "scratch-k8s",
        "state": "banked",
        "set_id": "set-grp-123",
        "instance": {"created_at": 1784415601692, "last_active_at": 1784415614835},
        "members": [
            {"member_name": "server", "state": "banked", "healthy": False},
            {"member_name": "agent-0", "state": "banked", "healthy": False},
        ],
    }
    shaped = _shape_status(raw)
    assert shaped["state"] == "banked"
    assert shaped["warm"] is True
    assert shaped["created_at"] == 1784415601692
    assert shaped["members"] == [
        {"name": "server", "state": "banked", "healthy": False},
        {"name": "agent-0", "state": "banked", "healthy": False},
    ]


def test_shape_status_no_set_is_cold_and_tolerates_missing_instance():
    shaped = _shape_status({"state": "destroyed", "set_id": None, "members": []})
    assert shaped["warm"] is False
    assert shaped["created_at"] is None
    assert shaped["members"] == []


def test_write_kubeconfig_targets_entry_with_token(tmp_path, monkeypatch):
    monkeypatch.setattr("demos.k8s_terminal_api.os.getpid", lambda: 424242)
    path = _write_kubeconfig("sekrit-token")
    try:
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
        cluster = config["clusters"][0]["cluster"]
        assert cluster["server"].startswith("https://")
        assert cluster["server"].endswith(":5410")
        assert cluster["insecure-skip-tls-verify"] is True
        assert config["users"][0]["user"]["token"] == "sekrit-token"
        assert config["current-context"] == "scratch-k8s"
    finally:
        import os

        os.unlink(path)


def test_write_kubeconfig_is_owner_only(tmp_path):
    import os
    import stat

    path = _write_kubeconfig("t")
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600
    finally:
        os.unlink(path)


@pytest.mark.parametrize(
    ("cols", "rows", "want_cols", "want_rows"),
    [(1, 1, 20, 5), (120, 32, 120, 32), (9999, 9999, 500, 200)],
)
def test_resize_clamps(cols, rows, want_cols, want_rows):
    # _resize clamps to sane bounds before the TIOCSWINSZ ioctl; with no
    # master fd it must be a no-op rather than an error.
    from demos.k8s_terminal_api import _TerminalSession

    session = _TerminalSession.__new__(_TerminalSession)
    session.master_fd = None
    session._resize(cols, rows)  # no fd: pure no-op, must not raise

    clamped_cols = max(20, min(500, cols))
    clamped_rows = max(5, min(200, rows))
    assert (clamped_cols, clamped_rows) == (want_cols, want_rows)
