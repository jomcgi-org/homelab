from types import SimpleNamespace

from tools.session_collector.scope import (
    allowed_scope,
    discover_repo,
    normalize_origin,
    parse_allowlist,
)


def test_normalizes_ssh_and_https_origins():
    assert normalize_origin("git@github.com:jomcgi-org/homelab.git") == (
        "jomcgi-org/homelab"
    )
    assert normalize_origin("https://github.com/jomcgi/homelab.git") == (
        "jomcgi/homelab"
    )
    assert normalize_origin("ssh://git@github.com/jomcgi/homelab.git") == (
        "jomcgi/homelab"
    )


def test_discovers_origin_for_existing_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tools.session_collector.scope.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="git@github.com:jomcgi-org/homelab.git\n"
        ),
    )
    assert discover_repo(str(tmp_path), {}) == "jomcgi-org/homelab"


def test_path_fallback_and_remembered_mapping(tmp_path):
    missing = tmp_path / "homelab" / "deleted"
    assert discover_repo(str(missing), {}) == "jomcgi-org/homelab"
    state = {"old": {"cwd": str(tmp_path / "other"), "repo": "owner/repo"}}
    assert discover_repo(str(tmp_path / "other"), state) == "owner/repo"


def test_allowlist_defaults_and_custom_values():
    defaults = parse_allowlist(None)
    assert defaults["jomcgi/homelab"] == "repo:jomcgi-org/homelab"
    custom = parse_allowlist(["owner/repo=repo:owner/repo"])
    assert custom == {"owner/repo": "repo:owner/repo"}
    assert allowed_scope("outside/repo", defaults) is None
